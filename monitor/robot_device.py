from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from uuid import uuid4


SUPPORTED_AUDIO_FORMAT = "opus"
SUPPORTED_SAMPLE_RATE = 16000
SUPPORTED_CHANNELS = 1


@dataclass
class DeviceSession:
    device_id: str
    client_id: str
    session_id: str
    connected_at: float
    last_seen_at: float
    state: str = "idle"
    wake_word: str = "小帮"
    input_format: str = SUPPORTED_AUDIO_FORMAT
    local_wake_ack: bool = False
    realtime_pcm: bool = False
    voice_capable: bool = True
    web_client: bool = False


class XiaozhiProtocolError(ValueError):
    pass


class XiaozhiDeviceRegistry:
    """Tracks the physical voice endpoint independently of the web dashboard."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        endpoint_silence_ms: int = 450,
        conversation_timeout_seconds: float = 10,
    ):
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.endpoint_silence_ms = max(300, min(int(endpoint_silence_ms), 900))
        self.conversation_timeout_seconds = max(1.0, min(float(conversation_timeout_seconds), 60.0))
        self._lock = threading.RLock()
        self._sessions: dict[str, DeviceSession] = {}

    @staticmethod
    def _required_text(value: object, field: str, maximum: int = 160) -> str:
        text = str(value or "").strip()
        if not text or len(text) > maximum:
            raise XiaozhiProtocolError(f"设备字段 {field} 无效")
        return text

    def hello(self, payload: dict, *, device_id: str, client_id: str) -> dict:
        if payload.get("type") != "hello" or payload.get("transport") != "websocket":
            raise XiaozhiProtocolError("设备握手格式不正确")
        if int(payload.get("version", 0)) != 1:
            raise XiaozhiProtocolError("暂不支持这个设备协议版本")
        audio = payload.get("audio_params")
        if not isinstance(audio, dict):
            raise XiaozhiProtocolError("设备没有声明音频参数")
        input_format = str(audio.get("format", "")).lower()
        features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
        body_only = features.get("body_only") is True and input_format == "none"
        realtime_pcm = input_format == "pcm" and (
            features.get("simulator") is True
            or features.get("realtime_pcm") is True
        )
        if (
            (input_format != SUPPORTED_AUDIO_FORMAT and not realtime_pcm and not body_only)
            or (
                not body_only
                and (
                    int(audio.get("sample_rate", 0)) != SUPPORTED_SAMPLE_RATE
                    or int(audio.get("channels", 0)) != SUPPORTED_CHANNELS
                )
            )
        ):
            raise XiaozhiProtocolError("实体音频必须是 16kHz 单声道 Opus")

        now = time.monotonic()
        session = DeviceSession(
            device_id=self._required_text(device_id, "Device-Id"),
            client_id=self._required_text(client_id, "Client-Id"),
            session_id=uuid4().hex,
            connected_at=now,
            last_seen_at=now,
            input_format=input_format,
            local_wake_ack=features.get("local_wake_ack") is True,
            realtime_pcm=realtime_pcm,
            voice_capable=not body_only,
            web_client=features.get("web_client") is True,
        )
        with self._lock:
            self._sessions[session.device_id] = session
        return {
            "type": "hello",
            "transport": "websocket",
            "session_id": session.session_id,
            "audio_params": {
                "format": SUPPORTED_AUDIO_FORMAT,
                "sample_rate": 24000,
                "channels": SUPPORTED_CHANNELS,
                "frame_duration": 60,
            },
            "features": {
                "local_wake_ack": True,
                "realtime_pcm": True,
                "endpoint_silence_ms": self.endpoint_silence_ms,
                "conversation_timeout_ms": int(self.conversation_timeout_seconds * 1000),
            },
        }

    def event(self, device_id: str, payload: dict) -> DeviceSession:
        with self._lock:
            session = self._sessions.get(str(device_id))
            if session is None:
                raise XiaozhiProtocolError("实体设备尚未完成握手")
            if str(payload.get("session_id", "")) != session.session_id:
                raise XiaozhiProtocolError("设备会话编号不匹配")
            message_type = str(payload.get("type", ""))
            if message_type == "listen":
                state = str(payload.get("state", ""))
                if state == "detect":
                    wake_text = str(payload.get("text", "")).replace(" ", "")
                    if "小帮" not in wake_text:
                        raise XiaozhiProtocolError("设备唤醒词不是小帮")
                    session.state = "listening"
                elif state == "start":
                    session.state = "listening"
                elif state == "stop":
                    session.state = "thinking"
                else:
                    raise XiaozhiProtocolError("设备监听状态无效")
            elif message_type == "abort":
                session.state = "idle"
            elif message_type == "heartbeat":
                pass
            else:
                raise XiaozhiProtocolError("暂不支持这个设备消息")
            session.last_seen_at = time.monotonic()
            return session

    def set_state(self, device_id: str, state: str) -> None:
        if state not in {"idle", "listening", "thinking", "speaking"}:
            raise XiaozhiProtocolError("设备状态无效")
        with self._lock:
            session = self._sessions.get(str(device_id))
            if session is None:
                raise XiaozhiProtocolError("实体设备不在线")
            session.state = state
            session.last_seen_at = time.monotonic()

    def touch(self, device_id: str) -> None:
        with self._lock:
            session = self._sessions.get(str(device_id))
            if session is None:
                raise XiaozhiProtocolError("实体设备不在线")
            session.last_seen_at = time.monotonic()

    def disconnect(self, device_id: str) -> None:
        with self._lock:
            self._sessions.pop(str(device_id), None)

    def _active_locked(self) -> list[DeviceSession]:
        now = time.monotonic()
        expired = [
            device_id
            for device_id, session in self._sessions.items()
            if now - session.last_seen_at > self.timeout_seconds
        ]
        for device_id in expired:
            self._sessions.pop(device_id, None)
        return sorted(
            self._sessions.values(),
            key=lambda item: item.last_seen_at,
            reverse=True,
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            active = self._active_locked()
            physical = [session for session in active if not session.web_client]
            voice_devices = [session for session in physical if session.voice_capable]
            primary = voice_devices[0] if voice_devices else None
            body_primary = physical[0] if physical else None
            return {
                "physical_online": body_primary is not None,
                "voice_source": "physical" if primary is not None else "web",
                "device_id": body_primary.device_id if body_primary else "",
                "state": body_primary.state if body_primary else "offline",
                "connected_devices": len(physical),
                "voice_capable_devices": len(voice_devices),
                "web_voice_clients": sum(1 for session in active if session.web_client),
                "protocol": "xiaozhi-websocket-v1",
                "realtime_pcm": bool(primary and primary.realtime_pcm),
                "local_wake_ack": bool(primary and primary.local_wake_ack),
                "endpoint_silence_ms": self.endpoint_silence_ms,
                "conversation_timeout_ms": int(self.conversation_timeout_seconds * 1000),
            }
