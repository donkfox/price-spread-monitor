from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import requests

from .robot_noise import is_noise_transcript
from .robot_opus import transcode_audio_to_opus_packets
from .secure_store import SecretProtectionError, unprotect_named_secret
from .robot_voiceprint_local import LocalVoiceprintEngine


SUPPORTED_AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}

DEFAULT_VOICE = "zh-CN-YunyangNeural"


class VoiceprintVerifier:
    """Pluggable speaker-verification adapter.

    The robot never treats the wake word itself as a voiceprint. A configured
    provider must explicitly return a match (or a score above the threshold).
    """

    def __init__(self, settings: dict | None, root: Path, logger):
        self.settings = settings if isinstance(settings, dict) else {}
        self.root = root
        self.log = logger
        self._http_session = requests.Session()
        self._local_engine = None
        if str(self.settings.get("provider", "external_http")).strip().lower() == "local":
            self._local_engine = LocalVoiceprintEngine(self.settings, root, logger)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def required_for_wake(self) -> bool:
        return bool(self.settings.get("required_for_wake", False))

    @property
    def endpoint(self) -> str:
        return str(self.settings.get("endpoint", "")).strip()

    def status(self) -> dict[str, object]:
        if self._local_engine is not None:
            return {
                **self._local_engine.status(),
                "required_for_wake": self.required_for_wake,
            }
        return {
            "enabled": self.enabled,
            "required_for_wake": self.required_for_wake,
            "provider": str(self.settings.get("provider", "external_http")),
            "configured": bool(self.endpoint),
            "enrolled": bool(str(self.settings.get("enrollment_id", "")).strip()),
        }

    def preload(self) -> None:
        if self._local_engine is not None:
            self._local_engine.preload()

    def _token(self) -> str:
        configured = str(self.settings.get("encrypted_token_file", "")).strip()
        if not configured:
            return ""
        path = self.root / configured
        if not path.is_file():
            return ""
        try:
            return unprotect_named_secret(
                path.read_text(encoding="utf-8").strip(),
                "robot-voiceprint",
            )
        except (OSError, SecretProtectionError) as error:
            self.log.warning("声纹服务令牌读取失败：%s", error)
            return ""

    def _request(self, action: str, audio: bytes, content_type: str) -> dict[str, object]:
        if not self.enabled:
            return {"verified": True, "available": False, "reason": "disabled"}
        if self._local_engine is not None:
            if action == "enroll":
                return self._local_engine.enroll(audio, content_type)
            return self._local_engine.verify(audio, content_type)
        if str(self.settings.get("provider", "external_http")).strip().lower() != "external_http":
            return {"verified": False, "available": False, "reason": "unsupported_provider"}
        if not self.endpoint:
            return {"verified": False, "available": False, "reason": "endpoint_not_configured"}
        payload = {
            "action": action,
            "audio_base64": base64.b64encode(audio).decode("ascii"),
            "content_type": content_type.split(";", 1)[0].strip().lower(),
            "enrollment_id": str(self.settings.get("enrollment_id", "")).strip(),
        }
        headers = {"Content-Type": "application/json"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._http_session.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=max(1.0, min(float(self.settings.get("timeout_seconds", 3)), 15.0)),
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("声纹服务返回格式无效")
            score = result.get("score")
            try:
                score_value = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_value = None
            verified = bool(result.get("verified", result.get("matched", False)))
            threshold = float(self.settings.get("threshold", 0.78))
            if score_value is not None:
                verified = verified or score_value >= threshold
            return {
                "verified": verified,
                "available": True,
                "score": score_value,
                "reason": "matched" if verified else "not_matched",
                "enrollment_id": str(result.get("enrollment_id", "")).strip(),
            }
        except (OSError, ValueError, requests.RequestException) as error:
            self.log.warning("声纹服务请求失败：%s", error)
            return {"verified": False, "available": False, "reason": "provider_error"}

    def verify(self, audio: bytes, content_type: str) -> dict[str, object]:
        return self._request("verify", audio, content_type)

    def enroll(self, audio: bytes, content_type: str) -> dict[str, object]:
        return self._request("enroll", audio, content_type)


class SpeechSynthesizer:
    def __init__(self, settings: dict, cache_dir: Path, logger, root: Path | None = None):
        self.settings = settings
        self.cache_dir = cache_dir
        self.log = logger
        self.root = root or cache_dir.parent.parent
        self._lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stream_requests: dict[str, tuple[str, float]] = {}
        self._http_session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    @property
    def voice(self) -> str:
        return str(self.settings.get("voice", DEFAULT_VOICE))

    @property
    def provider(self) -> str:
        return str(self.settings.get("provider", "edge")).strip().lower()

    @property
    def streaming_enabled(self) -> bool:
        return self.provider in {"bailian", "edge", "sapi"} and bool(
            self.settings.get("streaming_enabled", True)
        )

    @property
    def output_content_type(self) -> str:
        return "audio/wav" if self.provider == "sapi" else "audio/mpeg"

    def _prepared_text(self, text: str) -> str:
        value = self._spoken_text(text)
        if not value:
            raise ValueError("没有可朗读的内容")
        if len(value) > 1200:
            raise ValueError("朗读内容过长")
        if not self.enabled:
            raise RuntimeError("语音输出未启用")
        return value

    def _cache_target(self, value: str) -> Path:
        digest = hashlib.sha256(
            (
                f"{self.provider}\0{self.settings.get('model', '')}\0{self.voice}\0"
                f"{self.settings.get('instruction', '')}\0"
                f"{self.settings.get('sapi_rate', 2)}\0"
                f"{self.settings.get('rate', '-5%')}\0"
                f"{self.settings.get('pitch', '-2Hz')}\0"
                f"{self.settings.get('volume', '+0%')}\0{value}"
            ).encode("utf-8")
        ).hexdigest()
        extension = ".wav" if self.provider == "sapi" else ".mp3"
        return self.cache_dir / f"{digest}{extension}"

    def synthesize(self, text: str) -> Path:
        value = self._prepared_text(text)
        target = self._cache_target(value)
        digest = target.stem
        if target.is_file() and target.stat().st_size > 0:
            return target
        with self._lock:
            if target.is_file() and target.stat().st_size > 0:
                return target
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".tmp{target.suffix}")
            temporary.unlink(missing_ok=True)
            try:
                if self.provider == "sapi":
                    self._synthesize_sapi(value, temporary)
                elif self.provider == "bailian":
                    self._synthesize_bailian(value, temporary)
                else:
                    self._synthesize_edge(value, temporary)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError("语音服务没有返回音频")
                temporary.replace(target)
            except Exception as error:
                temporary.unlink(missing_ok=True)
                if self.provider == "bailian" and bool(
                    self.settings.get("fallback_enabled", True)
                ):
                    self.log.warning("百炼语音合成失败，改用备用声音：%s", error)
                    try:
                        self._synthesize_edge(value, temporary, fallback=True)
                        if not temporary.is_file() or temporary.stat().st_size == 0:
                            raise RuntimeError("备用语音服务没有返回音频")
                        fallback_target = self.cache_dir / f"{digest}.fallback.mp3"
                        temporary.replace(fallback_target)
                        target = fallback_target
                    except Exception as fallback_error:
                        temporary.unlink(missing_ok=True)
                        self.log.warning("备用语音合成失败：%s", fallback_error)
                        raise RuntimeError("语音合成暂时不可用，请稍后重试") from fallback_error
                else:
                    self.log.warning("语音合成失败：%s", error)
                    raise RuntimeError("语音合成暂时不可用，请稍后重试") from error
        self._prune_cache()
        return target

    def prepare_stream(self, text: str) -> str:
        value = self._prepared_text(text)
        if not self.streaming_enabled:
            raise RuntimeError("当前语音服务未启用流式播放")
        token = uuid4().hex
        now = time.monotonic()
        with self._stream_lock:
            self._stream_requests = {
                key: item
                for key, item in self._stream_requests.items()
                if now - item[1] < 120
            }
            while len(self._stream_requests) >= 64:
                oldest = min(self._stream_requests, key=lambda key: self._stream_requests[key][1])
                self._stream_requests.pop(oldest, None)
            self._stream_requests[token] = (value, now)
        return token

    def take_stream(self, token: str) -> str:
        with self._stream_lock:
            item = self._stream_requests.pop(token, None)
        if item is None or time.monotonic() - item[1] >= 120:
            raise ValueError("语音播放地址已失效")
        return item[0]

    @staticmethod
    def _file_chunks(path: Path, size: int = 32 * 1024):
        with path.open("rb") as source:
            while chunk := source.read(size):
                yield chunk

    def stream_text(self, value: str):
        target = self._cache_target(value)
        if target.is_file() and target.stat().st_size > 0:
            yield from self._file_chunks(target)
            return
        if not self.streaming_enabled:
            yield from self._file_chunks(self.synthesize(value))
            return
        if self.provider == "sapi":
            yield from self._file_chunks(self.synthesize(value))
            return
        if self.provider == "edge":
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                if target.is_file() and target.stat().st_size > 0:
                    yield from self._file_chunks(target)
                    return
                temporary = target.with_suffix(".tmp.mp3")
                temporary.unlink(missing_ok=True)
                received = False
                parts: list[bytes] = []
                try:
                    for chunk in self._stream_edge(value):
                        received = True
                        parts.append(chunk)
                        yield chunk
                    if not received:
                        raise RuntimeError("Edge 语音没有返回音频")
                    temporary.write_bytes(b"".join(parts))
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            self._prune_cache()
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if target.is_file() and target.stat().st_size > 0:
                yield from self._file_chunks(target)
                return
            received = False
            parts: list[bytes] = []
            try:
                for chunk in self._stream_bailian(value):
                    received = True
                    parts.append(chunk)
                    yield chunk
                if not received:
                    raise RuntimeError("百炼流式语音没有返回音频")
                target.write_bytes(b"".join(parts))
            except Exception as error:
                if received or not bool(self.settings.get("fallback_enabled", True)):
                    self.log.warning("百炼流式语音中断：%s", error)
                    raise
                self.log.warning("百炼流式语音失败，改用备用声音：%s", error)
                fallback = target.with_suffix(".fallback.mp3")
                fallback.unlink(missing_ok=True)
                self._synthesize_edge(value, fallback, fallback=True)
                yield from self._file_chunks(fallback)
        self._prune_cache()

    def stream_opus(self, text: str):
        value = self._prepared_text(text)
        yield from transcode_audio_to_opus_packets(
            self.stream_text(value),
            input_format="wav" if self.provider == "sapi" else "mp3",
            sample_rate=24000,
            frame_duration_ms=60,
        )

    def _synthesize_sapi(self, text: str, target: Path) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("没有找到 Windows 本地语音组件")
        environment = os.environ.copy()
        environment["XIAOBANG_TTS_TEXT"] = text
        environment["XIAOBANG_TTS_TARGET"] = str(target)
        environment["XIAOBANG_TTS_VOICE"] = self.voice
        environment["XIAOBANG_TTS_RATE"] = str(
            max(-10, min(int(self.settings.get("sapi_rate", 2)), 10))
        )
        script = (
            "Add-Type -AssemblyName System.Speech\n"
            "$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "$speech.SelectVoice($env:XIAOBANG_TTS_VOICE)\n"
            "$speech.Rate = [int]$env:XIAOBANG_TTS_RATE\n"
            "$speech.SetOutputToWaveFile($env:XIAOBANG_TTS_TARGET)\n"
            "$speech.Speak($env:XIAOBANG_TTS_TEXT)\n"
            "$speech.Dispose()\n"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(message or "Windows 本地语音合成失败")

    def _synthesize_edge(self, text: str, target: Path, fallback: bool = False) -> None:
        try:
            import edge_tts
        except ImportError as error:
            raise RuntimeError("缺少 edge-tts，暂时不能朗读") from error
        voice = (
            str(self.settings.get("fallback_voice", DEFAULT_VOICE))
            if fallback
            else self.voice
        )
        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=str(self.settings.get("rate", "-5%")),
            pitch=str(self.settings.get("pitch", "-2Hz")),
            volume=str(self.settings.get("volume", "+0%")),
        )
        asyncio.run(communicate.save(str(target)))

    def _stream_edge(self, text: str):
        try:
            import edge_tts
        except ImportError as error:
            raise RuntimeError("缺少 edge-tts，暂时不能朗读") from error

        output: queue.Queue[bytes | object] = queue.Queue()
        finished = object()
        errors: list[Exception] = []
        cancelled = threading.Event()

        async def produce() -> None:
            try:
                communicate = edge_tts.Communicate(
                    text,
                    self.voice,
                    rate=str(self.settings.get("rate", "-5%")),
                    pitch=str(self.settings.get("pitch", "-2Hz")),
                    volume=str(self.settings.get("volume", "+0%")),
                )
                async for event in communicate.stream():
                    if cancelled.is_set():
                        break
                    if event.get("type") == "audio" and event.get("data"):
                        output.put(bytes(event["data"]))
            except Exception as error:
                errors.append(error)
            finally:
                output.put(finished)

        worker = threading.Thread(
            target=lambda: asyncio.run(produce()),
            name="xiaobang-edge-tts",
            daemon=True,
        )
        worker.start()
        try:
            while True:
                item = output.get()
                if item is finished:
                    break
                yield item
            if errors:
                raise RuntimeError(f"Edge 语音流中断：{errors[0]}") from errors[0]
        finally:
            cancelled.set()
            worker.join(timeout=2)

    def _api_key(self) -> str:
        encrypted_path = self.root / str(
            self.settings.get("encrypted_key_file", "secrets/robot_dialogue_key.dat")
        )
        if encrypted_path.is_file():
            try:
                return unprotect_named_secret(
                    encrypted_path.read_text(encoding="ascii").strip(),
                    "robot-dialogue-key",
                )
            except (OSError, SecretProtectionError) as error:
                raise RuntimeError("百炼语音密钥无法读取") from error
        legacy = self.root / str(
            self.settings.get("api_key_file", "secrets/robot_api_key.txt")
        )
        if legacy.is_file():
            return legacy.read_text(encoding="utf-8-sig").strip()
        raise RuntimeError("没有配置百炼语音密钥")

    def _bailian_request(self, text: str) -> tuple[str, dict, dict, float]:
        endpoint = str(self.settings.get("endpoint", "")).strip()
        if not endpoint:
            endpoint = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
        model = str(self.settings.get("model", "")).strip()
        if not model or not self.voice:
            raise RuntimeError("百炼语音配置不完整")
        api_key = self._api_key()
        payload = {
            "model": model,
            "input": {
                "text": text,
                "voice": self.voice,
                "format": "mp3",
                "sample_rate": 24000,
                "instruction": str(self.settings.get("instruction", "")).strip(),
            },
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        workspace = str(self.settings.get("workspace", "")).strip()
        if workspace:
            headers["X-DashScope-Workspace"] = workspace
        timeout = float(self.settings.get("timeout_seconds", 45))
        return endpoint, payload, headers, timeout

    def _synthesize_bailian(self, text: str, target: Path) -> None:
        endpoint, payload, headers, timeout = self._bailian_request(text)
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            audio_url = str(result.get("output", {}).get("audio", {}).get("url", ""))
            if not audio_url:
                raise RuntimeError("百炼没有返回音频地址")
            with urlopen(audio_url, timeout=timeout) as response:
                target.write_bytes(response.read())
        except HTTPError as error:
            raise RuntimeError(f"百炼语音接口返回 HTTP {error.code}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("百炼语音网络请求失败") from error

    def _stream_bailian(self, text: str):
        endpoint, payload, headers, timeout = self._bailian_request(text)
        headers["X-DashScope-SSE"] = "enable"
        try:
            with self._http_session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
                stream=True,
            ) as response:
                response.raise_for_status()
                for raw_line in response.iter_lines():
                    if not raw_line or not raw_line.startswith(b"data:"):
                        continue
                    event = json.loads(raw_line[5:].strip())
                    encoded = event.get("output", {}).get("audio", {}).get("data")
                    if encoded:
                        yield base64.b64decode(encoded)
        except (requests.RequestException, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("百炼流式语音网络请求失败") from error

    @staticmethod
    def _spoken_text(text: str) -> str:
        value = text.strip().replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"```.*?```", "", value, flags=re.DOTALL)
        value = re.sub(r"https?://\S+", "链接", value)
        value = re.sub(r"[*_#>`]", "", value)
        value = re.sub(r"(?m)^\s*(?:[-•]|\d+[.、])\s*", "", value)
        value = re.sub(r"¥\s*([0-9]+(?:\.[0-9]+)?)", r"\1 元", value)
        lines = [" ".join(line.split()) for line in value.split("\n")]
        spoken_lines = []
        for line in lines:
            if not line:
                continue
            if not re.search(r"[。！？!?；;，,：:]$", line):
                line += "。"
            spoken_lines.append(line)
        value = "\n".join(spoken_lines)
        return re.sub(r"\n{3,}", "\n\n", value).strip()

    def _prune_cache(self) -> None:
        files = sorted(
            [*self.cache_dir.glob("*.mp3"), *self.cache_dir.glob("*.wav")],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in files[int(self.settings.get("cache_files", 120)):]:
            stale.unlink(missing_ok=True)


class SpeechRecognizer:
    def __init__(self, settings: dict, model_root: Path, work_dir: Path, logger):
        self.settings = settings
        self.model_root = model_root
        self.work_dir = work_dir
        self.log = logger
        self._model = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self.voiceprint = VoiceprintVerifier(
            settings.get("voiceprint", {}),
            work_dir.parent.parent,
            logger,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def transcribe(
        self,
        audio: bytes,
        content_type: str,
        *,
        allow_empty: bool = False,
        hotwords: str | None = None,
        beam_size: int | None = None,
        initial_prompt: str | None = None,
        normalize: bool = True,
    ) -> str:
        if not self.enabled:
            raise RuntimeError("语音识别未启用")
        media_type = content_type.split(";", 1)[0].strip().lower()
        suffix = SUPPORTED_AUDIO_TYPES.get(media_type)
        if suffix is None:
            raise ValueError("不支持这种录音格式")
        max_bytes = int(self.settings.get("max_upload_bytes", 10 * 1024 * 1024))
        if not audio or len(audio) > max_bytes:
            raise ValueError("录音为空或超过大小限制")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("没有找到 FFmpeg，暂时不能识别录音")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        source = self.work_dir / f"{token}.source{suffix}"
        wave = self.work_dir / f"{token}.wav"
        try:
            source.write_bytes(audio)
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(wave),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not wave.is_file():
                raise RuntimeError("录音格式转换失败")
            model = self._load_model()
            with self._inference_lock:
                segments, _ = model.transcribe(
                    str(wave),
                    language=str(self.settings.get("language", "zh")),
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": int(
                            self.settings.get("vad_min_silence_ms", 250)
                        ),
                        "speech_pad_ms": int(self.settings.get("vad_speech_pad_ms", 120)),
                    },
                    beam_size=(
                        int(beam_size)
                        if beam_size is not None
                        else int(self.settings.get("beam_size", 3))
                    ),
                    hotwords=(
                        hotwords
                        if hotwords is not None
                        else str(self.settings.get("hotwords", "")).strip() or None
                    ),
                    initial_prompt=(
                        initial_prompt
                        if initial_prompt is not None
                        else str(self.settings.get("initial_prompt", "")).strip() or None
                    ),
                    condition_on_previous_text=False,
                    without_timestamps=True,
                )
            text = "".join(segment.text for segment in segments).strip()
            if not text:
                if allow_empty:
                    return ""
                raise ValueError("没有听清，请靠近麦克风再说一次")
            if normalize:
                text = self._normalize_transcript(text)
            return text[:2000]
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("录音处理超时") from error
        finally:
            source.unlink(missing_ok=True)
            wave.unlink(missing_ok=True)

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError("本地语音识别组件尚未安装") from error
            self.model_root.mkdir(parents=True, exist_ok=True)
            model_name = str(self.settings.get("model", "small"))
            self.log.info("正在加载本地语音识别模型 %s", model_name)
            self._model = WhisperModel(
                model_name,
                device=str(self.settings.get("device", "cpu")),
                compute_type=str(self.settings.get("compute_type", "int8")),
                download_root=str(self.model_root),
            )
            return self._model

    def preload(self) -> None:
        if not self.enabled:
            return
        self.voiceprint.preload()
        try:
            self._load_model()
            self.log.info("本地语音识别模型已预热")
        except Exception as error:
            self.log.warning("本地语音识别模型预热失败：%s", error)

    def _normalize_transcript(self, text: str) -> str:
        value = str(text or "").strip()
        replacements = self.settings.get("normalization_replacements", {})
        if not isinstance(replacements, dict):
            return value
        for source, target in replacements.items():
            source_text = str(source).strip()
            if source_text:
                value = value.replace(source_text, str(target))
        return value

    @staticmethod
    def extract_wake_command(text: str, wake_word: str = "小帮") -> tuple[bool, str]:
        value = str(text or "")
        word = str(wake_word or "小帮").strip() or "小帮"
        pattern = r"\s*".join(re.escape(character) for character in word)
        match = re.search(pattern, value)
        compact_prefix = re.sub(r"[\s，,。.!！?？:：;；、]", "", value[:match.start()] if match else value)
        if match is None or len(compact_prefix) > 6:
            return False, ""
        command = value[match.end():].lstrip(" ，,。.!！?？:：;；、\t\r\n")
        return True, command

    def detect_wake(self, audio: bytes, content_type: str) -> dict[str, object]:
        wake_word = str(self.settings.get("wake_word", "小帮")).strip() or "小帮"
        text = self._normalize_transcript(
            self.transcribe(
                audio,
                content_type,
                allow_empty=True,
                hotwords=wake_word,
                beam_size=int(self.settings.get("wake_beam_size", 3)),
                initial_prompt="",
                normalize=False,
            )
        )
        matched, command = self.extract_wake_command(text, wake_word)
        if matched and is_noise_transcript(command):
            command = ""
        if matched and command and bool(self.settings.get("wake_recheck_commands", True)):
            refined_text = self._normalize_transcript(
                self.transcribe(
                    audio,
                    content_type,
                    allow_empty=True,
                    hotwords=wake_word,
                    beam_size=int(self.settings.get("wake_recheck_beam_size", 5)),
                    initial_prompt="",
                    normalize=False,
                )
            )
            refined_matched, refined_command = self.extract_wake_command(refined_text, wake_word)
            if not refined_matched:
                matched = False
                command = ""
            else:
                text = refined_text
                command = refined_command or command
        if is_noise_transcript(command):
            command = ""
        if matched and self.voiceprint.required_for_wake:
            voiceprint = self.voiceprint.verify(audio, content_type)
        elif matched:
            voiceprint = {
                "verified": None,
                "available": self.voiceprint.enabled,
                "score": None,
                "reason": "not_required",
            }
        else:
            voiceprint = {
                "verified": False,
                "available": False,
                "reason": "wake_not_matched",
            }
        if matched and self.voiceprint.required_for_wake and not voiceprint.get("verified", False):
            matched = False
            command = ""
        if text:
            self.log.info(
                "wake result: matched=%s text=%r command=%r voiceprint=%s score=%s reason=%s",
                matched,
                text,
                command,
                voiceprint.get("verified"),
                voiceprint.get("score"),
                voiceprint.get("reason"),
            )
        return {
            "matched": matched,
            "text": text,
            "wake_word": wake_word,
            "command": command,
            "voiceprint": {
                **voiceprint,
                "required": self.voiceprint.required_for_wake,
            },
        }

    def voiceprint_status(self) -> dict[str, object]:
        return self.voiceprint.status()

    def enroll_voiceprint(self, audio: bytes, content_type: str) -> dict[str, object]:
        return self.voiceprint.enroll(audio, content_type)

    def _last_physical_voiceprint_file(self) -> Path:
        return self.work_dir.parent.parent / "runtime" / "voiceprint-enroll-last.wav"

    def last_physical_voiceprint_sample(self) -> dict[str, object]:
        target = self._last_physical_voiceprint_file()
        if not target.is_file():
            return {"available": False, "reason": "not_found"}
        metadata: dict = {}
        sidecar = target.with_suffix(".json")
        if sidecar.is_file():
            try:
                value = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    metadata = value
            except (OSError, ValueError):
                metadata = {}
        return {
            "available": True,
            "created_at": str(metadata.get("created_at", "")),
            "duration_ms": metadata.get("duration_ms"),
            "text": str(metadata.get("text", "")),
            "wake_matched": bool(metadata.get("wake_matched")),
            "voiceprint_verified": bool(metadata.get("voiceprint_verified")),
            "voiceprint_score": metadata.get("voiceprint_score"),
            "voiceprint_reason": str(metadata.get("voiceprint_reason", "")),
            "size": target.stat().st_size,
        }

    def enroll_last_physical_voiceprint(self) -> dict[str, object]:
        target = self._last_physical_voiceprint_file()
        if not target.is_file():
            return {
                "verified": False,
                "available": False,
                "reason": "not_found",
                "error": "还没有捕捉到实体“小帮”录音",
            }
        return self.enroll_voiceprint(target.read_bytes(), "audio/wav")
