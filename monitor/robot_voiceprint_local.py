from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import numpy as np


class LocalVoiceprintEngine:
    """On-device speaker verification with a SpeechBrain ECAPA model."""

    def __init__(self, settings: dict, root: Path, logger):
        self.settings = settings if isinstance(settings, dict) else {}
        self.root = root
        self.log = logger
        self._model = None
        self._model_lock = threading.Lock()
        self._store_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def required_samples(self) -> int:
        return max(1, int(self.settings.get("required_samples", 3)))

    @property
    def threshold(self) -> float:
        return float(self.settings.get("threshold", 0.75))

    @property
    def model_dir(self) -> Path:
        relative = str(self.settings.get("model_dir", "runtime/robot-models/voiceprint")).strip()
        path = Path(relative)
        return path if path.is_absolute() else (self.root / path)

    @property
    def store_path(self) -> Path:
        relative = str(self.settings.get("store_dir", "runtime/robot-voiceprint")).strip()
        path = Path(relative)
        if not path.is_absolute():
            path = self.root / path
        return path / "enrollment.json"

    @property
    def work_dir(self) -> Path:
        relative = str(self.settings.get("store_dir", "runtime/robot-voiceprint")).strip()
        path = Path(relative)
        return path if path.is_absolute() else (self.root / path)

    def status(self) -> dict[str, object]:
        enrollment = self._load_enrollment()
        samples = enrollment.get("samples", [])
        enrolled = bool(enrollment.get("enrolled", False))
        return {
            "enabled": self.enabled,
            "provider": "local",
            "configured": self._configured(),
            "enrolled": enrolled,
            "sample_count": len(samples),
            "required_samples": self.required_samples,
            "threshold": self.threshold,
            "created_at": enrollment.get("created_at", ""),
        }

    def _configured(self) -> bool:
        model_path = self.model_dir
        return (
            model_path.is_dir()
            and (model_path / "hyperparams.yaml").is_file()
            and (model_path / "embedding_model.ckpt").is_file()
        )

    def preload(self) -> None:
        try:
            self._load_model()
            self.log.info("本地声纹模型已预热")
        except Exception as error:
            self.log.warning("本地声纹模型预热失败：%s", error)

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            if not self._configured():
                raise RuntimeError("本地声纹模型文件不完整")
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy

            model_dir = str(self.model_dir)
            self.log.info("正在加载本地声纹模型 %s", model_dir)
            self._model = EncoderClassifier.from_hparams(
                source=model_dir,
                savedir=model_dir,
                run_opts={"device": "cpu"},
                local_strategy=LocalStrategy.COPY_SKIP_CACHE,
            )
            return self._model

    def _transcode(self, audio: bytes, content_type: str) -> np.ndarray:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("没有找到 FFmpeg，暂时不能处理声纹录音")
        if not audio:
            raise ValueError("声纹录音为空")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        media_type = content_type.split(";", 1)[0].strip().lower()
        suffix = {
            "audio/webm": ".webm",
            "audio/ogg": ".ogg",
            "audio/mp4": ".m4a",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
        }.get(media_type, ".webm")
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
                    "-sample_fmt",
                    "s16",
                    str(wave),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not wave.is_file():
                raise RuntimeError("声纹录音格式转换失败")
            import wave as wave_module

            with wave_module.open(str(wave), "rb") as handle:
                if handle.getframerate() != 16000 or handle.getnchannels() != 1:
                    raise RuntimeError("声纹录音采样率异常")
                frames = handle.readframes(handle.getnframes())
                samples = (
                    np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                )
            return samples
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("声纹录音处理超时") from error
        finally:
            source.unlink(missing_ok=True)
            wave.unlink(missing_ok=True)

    def _embed(self, samples: np.ndarray) -> np.ndarray:
        duration = float(len(samples)) / 16000.0
        min_duration = float(self.settings.get("min_duration_seconds", 0.35))
        max_duration = float(self.settings.get("max_duration_seconds", 15))
        if duration < min_duration:
            raise ValueError(f"录音太短，请至少说 {min_duration:.0f} 秒")
        if duration > max_duration:
            raise ValueError(f"录音太长，最多 {max_duration:.0f} 秒")
        model = self._load_model()
        import torch

        waveform = torch.from_numpy(samples).unsqueeze(0)
        with torch.no_grad():
            batch = model.encode_batch(waveform)
        embedding = batch[0, 0].detach().cpu().numpy().astype(np.float64)
        norm = float(np.linalg.norm(embedding))
        if norm <= 0:
            raise RuntimeError("声纹特征提取失败")
        return (embedding / norm).astype(np.float64)

    def enroll(self, audio: bytes, content_type: str) -> dict[str, object]:
        if not self.enabled:
            return {"verified": False, "available": False, "reason": "disabled"}
        try:
            samples = self._transcode(audio, content_type)
            embedding = self._embed(samples)
        except Exception as error:
            self.log.warning("声纹登记失败：%s", error)
            return {
                "verified": False,
                "available": False,
                "reason": "enroll_error",
                "error": str(error),
            }
        with self._store_lock:
            enrollment = self._load_enrollment()
            pending = [] if enrollment.get("enrolled", False) else list(enrollment.get("samples", []))
            pending.append(embedding.tolist())
            pending = pending[-self.required_samples:]
            created_at = enrollment.get("created_at", "")
            if len(pending) >= self.required_samples:
                created_at = _now()
            enrolled = len(pending) >= self.required_samples
            profile = None
            if enrolled:
                profile = np.mean(np.asarray(pending, dtype=np.float64), axis=0)
                profile = profile / float(np.linalg.norm(profile))
            payload = {
                "enrolled": enrolled,
                "created_at": created_at,
                "required_samples": self.required_samples,
                "threshold": self.threshold,
                "samples": pending,
                "profile": profile.tolist() if profile is not None else None,
                "updated_at": _now(),
            }
            self._save_enrollment(payload)
            return {
                "verified": True,
                "available": True,
                "score": 1.0,
                "reason": "enrolled" if enrolled else "sample_saved",
                "enrolled": enrolled,
                "sample_count": len(pending),
                "required_samples": self.required_samples,
            }

    def verify(self, audio: bytes, content_type: str) -> dict[str, object]:
        if not self.enabled:
            return {"verified": True, "available": False, "reason": "disabled"}
        enrollment = self._load_enrollment()
        profile = enrollment.get("profile")
        if not enrollment.get("enrolled", False) or not isinstance(profile, list) or not profile:
            return {
                "verified": False,
                "available": True,
                "score": None,
                "reason": "not_enrolled",
            }
        try:
            samples = self._transcode(audio, content_type)
            embedding = self._embed(samples)
        except Exception as error:
            self.log.warning("声纹验证失败：%s", error)
            return {
                "verified": False,
                "available": False,
                "score": None,
                "reason": "verify_error",
                "error": str(error),
            }
        reference = np.asarray(profile, dtype=np.float64)
        score = float(np.dot(embedding, reference) / max(np.linalg.norm(embedding), 1e-12))
        score = max(-1.0, min(1.0, score))
        verified = score >= self.threshold
        return {
            "verified": verified,
            "available": True,
            "score": round(score, 4),
            "reason": "matched" if verified else "not_matched",
        }

    def _load_enrollment(self) -> dict:
        path = self.store_path
        if not path.is_file():
            return {
                "enrolled": False,
                "created_at": "",
                "required_samples": self.required_samples,
                "threshold": self.threshold,
                "samples": [],
                "profile": None,
            }
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.log.warning("声纹登记文件无法读取，按未登记处理")
            return {}

    def _save_enrollment(self, payload: dict) -> None:
        path = self.store_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
