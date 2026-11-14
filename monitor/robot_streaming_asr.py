from __future__ import annotations

import threading
import time
from collections.abc import Callable

from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from .robot_opus import OggOpusMuxer


class _StreamingCallback(RecognitionCallback):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sentences: dict[int, str] = {}
        self.error = ""

    def on_event(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        values = sentence if isinstance(sentence, list) else [sentence]
        with self._lock:
            for item in values:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                if text:
                    self._sentences[int(item.get("sentence_id", len(self._sentences) + 1))] = text

    def on_error(self, result: RecognitionResult) -> None:
        self.error = str(getattr(result, "message", "") or getattr(result, "code", "") or "流式语音识别失败")

    def text(self) -> str:
        with self._lock:
            return "".join(self._sentences[key] for key in sorted(self._sentences)).strip()


class DashScopeStreamingSession:
    def __init__(
        self,
        settings: dict,
        api_key_provider: Callable[[], str],
        *,
        audio_format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
        frame_duration_ms: int = 60,
        recognition_factory=Recognition,
    ):
        self.settings = settings
        self.api_key_provider = api_key_provider
        self.audio_format = str(audio_format).lower()
        if self.audio_format not in {"pcm", "opus"}:
            raise ValueError("实时识别只支持 PCM 或 Opus")
        if int(sample_rate) != 16000 or int(channels) != 1:
            raise ValueError("实时识别音频必须是 16kHz 单声道")
        self._opus_muxer = (
            OggOpusMuxer(
                sample_rate=sample_rate,
                channels=channels,
                frame_duration_ms=frame_duration_ms,
            )
            if self.audio_format == "opus"
            else None
        )
        self._callback = _StreamingCallback()
        self._lock = threading.RLock()
        self._recognition = recognition_factory(
            model=str(settings.get("model", "paraformer-realtime-v2")),
            callback=self._callback,
            format=self.audio_format,
            sample_rate=sample_rate,
            workspace=str(settings.get("workspace", "")).strip() or None,
            api_key=api_key_provider(),
            semantic_punctuation_enabled=bool(
                settings.get("semantic_punctuation_enabled", False)
            ),
            max_sentence_silence=int(settings.get("max_sentence_silence_ms", 400)),
        )
        self._started = False
        self._finished = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._recognition.start()
            if self._opus_muxer is not None:
                for header in self._opus_muxer.headers():
                    self._recognition.send_audio_frame(header)
            self._started = True

    def send(self, audio: bytes) -> None:
        with self._lock:
            if not self._started or self._finished or not audio:
                return
            if self._opus_muxer is not None:
                audio = self._opus_muxer.packet(audio)
            try:
                self._recognition.send_audio_frame(audio)
            except Exception as error:
                self._finished = True
                raise RuntimeError(f"云端流式识别发送失败：{error}") from error

    def finish(self) -> str:
        with self._lock:
            if not self._started or self._finished:
                if self._callback.error:
                    raise RuntimeError(self._callback.error)
                return self._callback.text()
            self._finished = True
            self._recognition.stop()
            if self._callback.error:
                raise RuntimeError(self._callback.error)
            text = self._callback.text()
            if not text:
                raise ValueError("没有听清，请靠近麦克风再说一次")
            return text

    def finish_fast(self, timeout_seconds: float = 0.8, stable_seconds: float = 0.12) -> str:
        """Return a stable streaming transcript without blocking on cloud teardown."""
        with self._lock:
            if not self._started or self._finished:
                if self._callback.error:
                    raise RuntimeError(self._callback.error)
                return self._callback.text()
            self._finished = True
            finished = threading.Event()

            def stop_recognition() -> None:
                try:
                    self._recognition.stop()
                finally:
                    finished.set()

            threading.Thread(
                target=stop_recognition,
                name="xiaobang-asr-stop",
                daemon=True,
            ).start()
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        last_text = ""
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            text = self._callback.text()
            if text != last_text:
                last_text = text
                stable_since = time.monotonic()
            if text and (
                finished.is_set()
                or time.monotonic() - stable_since >= max(0.05, float(stable_seconds))
            ):
                if self._callback.error:
                    raise RuntimeError(self._callback.error)
                return text
            time.sleep(0.02)
        text = self._callback.text()
        if self._callback.error:
            raise RuntimeError(self._callback.error)
        if not text:
            raise ValueError("没有听清，请靠近麦克风再说一次")
        return text

    def cancel(self) -> None:
        with self._lock:
            if not self._started or self._finished:
                return
            self._finished = True
            try:
                self._recognition.stop()
            except Exception:
                pass
