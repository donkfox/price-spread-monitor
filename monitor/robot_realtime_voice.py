from __future__ import annotations

import base64
import queue
import threading
import time
from collections.abc import Callable, Iterator

from dashscope.audio.qwen_omni import (
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)
from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams


class _RealtimeCallback(OmniRealtimeCallback):
    def __init__(self) -> None:
        self.session_updated = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.error = ""
        self.closed = False
        self.response_finished = False
        self.usage: dict = {}

    def begin_turn(self) -> None:
        self.response_finished = False
        self.usage = {}
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def on_event(self, message: dict) -> None:
        event_type = str(message.get("type", ""))
        if event_type == "session.updated":
            self.session_updated.set()
        elif event_type == "conversation.item.input_audio_transcription.completed":
            self.events.put(("transcript", str(message.get("transcript", "")).strip()))
        elif event_type == "response.audio_transcript.delta":
            self.events.put(("reply_delta", str(message.get("delta", ""))))
        elif event_type == "response.audio_transcript.done":
            transcript = str(message.get("transcript", "")).strip()
            if transcript:
                self.events.put(("reply_done", transcript))
        elif event_type == "response.audio.delta":
            delta = str(message.get("delta", ""))
            if delta:
                self.events.put(("audio", base64.b64decode(delta)))
        elif event_type == "response.done":
            response = message.get("response", {})
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                self.usage = response["usage"]
            self.response_finished = True
            self.events.put(("done", message.get("response", {})))
        elif event_type == "error":
            error = message.get("error", {})
            if isinstance(error, dict):
                self.error = str(error.get("message") or error.get("code") or error)
            else:
                self.error = str(error)
            self.session_updated.set()
            self.events.put(("error", self.error))

    def on_close(self, close_status_code, close_msg) -> None:
        self.closed = True
        if not self.response_finished and not self.error:
            self.error = f"realtime connection closed: {close_status_code} {close_msg}"
            self.session_updated.set()
            self.events.put(("error", self.error))


class QwenRealtimeVoiceSession:
    def __init__(
        self,
        settings: dict,
        api_key_provider: Callable[[], str],
        *,
        instructions: str,
        conversation_factory=OmniRealtimeConversation,
    ) -> None:
        self.settings = settings
        self.api_key_provider = api_key_provider
        self.instructions = instructions
        self.conversation_factory = conversation_factory
        self.callback = _RealtimeCallback()
        self.conversation = None
        self._started = False
        self._closed = False
        self._input_committed = False
        self._audio_chunks: list[bytes] = []
        self._pending: list[tuple[str, object]] = []
        self._reply_parts: list[str] = []
        self._final_reply = ""
        self._turn_number = 0
        self._turn_started_at: float | None = None
        self._input_committed_at: float | None = None
        self._transcript_at: float | None = None
        self._response_requested_at: float | None = None
        self._first_audio_at: float | None = None
        self._response_done_at: float | None = None

    @property
    def buffered_audio(self) -> tuple[bytes, ...]:
        return tuple(self._audio_chunks)

    @property
    def reply(self) -> str:
        return self._final_reply or "".join(self._reply_parts).strip()

    @property
    def usage(self) -> dict:
        return dict(self.callback.usage)

    @property
    def ready(self) -> bool:
        return (
            self._started
            and not self._closed
            and not self.callback.closed
            and not self.callback.error
        )

    @property
    def turn_number(self) -> int:
        return self._turn_number

    def turn_metrics(self) -> dict[str, float | int | None]:
        committed = self._input_committed_at
        transcript = self._transcript_at
        requested = self._response_requested_at
        first_audio = self._first_audio_at
        done = self._response_done_at
        return {
            "turn": self._turn_number,
            "speech_end_to_transcript_ms": (
                round((transcript - committed) * 1000, 1)
                if committed is not None and transcript is not None
                else None
            ),
            "transcript_to_first_audio_ms": (
                round((first_audio - transcript) * 1000, 1)
                if transcript is not None and first_audio is not None
                else None
            ),
            "speech_end_to_first_audio_ms": (
                round((first_audio - committed) * 1000, 1)
                if committed is not None and first_audio is not None
                else None
            ),
            "response_request_to_first_audio_ms": (
                round((first_audio - requested) * 1000, 1)
                if requested is not None and first_audio is not None
                else None
            ),
            "speech_end_to_done_ms": (
                round((done - committed) * 1000, 1)
                if committed is not None and done is not None
                else None
            ),
        }

    def start(self) -> None:
        if self._started:
            return
        self.conversation = self.conversation_factory(
            model=str(self.settings.get("model", "qwen3.5-omni-flash-realtime")),
            callback=self.callback,
            workspace=str(self.settings.get("workspace", "")).strip() or None,
            api_key=self.api_key_provider(),
        )
        self.conversation.connect()
        transcription_context = (
            str(self.settings.get("transcription_context", "")).strip() or None
            if bool(self.settings.get("use_transcription_context", False))
            else None
        )
        session_settings = {
            "output_modalities": [MultiModality.AUDIO, MultiModality.TEXT],
            "enable_input_audio_transcription": True,
            "input_audio_transcription_model": "qwen3-asr-flash-realtime",
            "transcription_params": TranscriptionParams(
                language="zh",
                sample_rate=16000,
                input_audio_format="pcm",
                corpus_text=transcription_context,
            ),
            "enable_turn_detection": False,
            "instructions": self.instructions,
        }
        # Qwen3.5-Omni-Realtime's default voice is Tina. Pass it explicitly:
        # the current service rejects a JSON null voice instead of applying
        # the documented default.
        voice = str(self.settings.get("voice", "")).strip()
        if voice:
            session_settings["voice"] = voice
        self.conversation.update_session(**session_settings)
        timeout = float(self.settings.get("connect_timeout_seconds", 6))
        if not self.callback.session_updated.wait(timeout):
            self.close()
            raise TimeoutError("实时语音会话连接超时")
        if self.callback.error:
            error = self.callback.error
            self.close()
            raise RuntimeError(error)
        self._started = True

    def begin_turn(self) -> None:
        if not self._started:
            self.start()
        if not self.ready:
            raise RuntimeError(self.callback.error or "实时语音连接已经断开")
        self.callback.begin_turn()
        self._input_committed = False
        self._audio_chunks = []
        self._pending = []
        self._reply_parts = []
        self._final_reply = ""
        self._turn_number += 1
        self._turn_started_at = time.perf_counter()
        self._input_committed_at = None
        self._transcript_at = None
        self._response_requested_at = None
        self._first_audio_at = None
        self._response_done_at = None

    def send(self, audio: bytes) -> None:
        if not self._started or self._closed or not audio:
            return
        value = bytes(audio)
        encoded = base64.b64encode(value).decode("ascii")
        try:
            self.conversation.append_audio(encoded)
        except Exception as error:
            self._closed = True
            self.callback.closed = True
            if not self.callback.error:
                self.callback.error = str(error)
            raise RuntimeError("实时语音连接已断开") from error
        self._audio_chunks.append(value)

    def commit_input(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("实时语音会话没有启动")
        if not self._input_committed:
            self.conversation.commit()
            self._input_committed = True
            self._input_committed_at = time.perf_counter()

    def finish_input(self) -> str:
        self.commit_input()
        timeout = float(self.settings.get("transcript_timeout_seconds", 5))
        while True:
            try:
                kind, payload = self.callback.events.get(timeout=timeout)
            except queue.Empty as error:
                raise TimeoutError("实时语音听写超时") from error
            if kind == "transcript":
                text = str(payload).strip()
                if not text:
                    raise ValueError("没有听清，请靠近麦克风再说一次")
                self._transcript_at = time.perf_counter()
                return text
            if kind == "error":
                raise RuntimeError(str(payload))
            self._pending.append((kind, payload))

    def request_response(self, instructions: str | None = None) -> None:
        if not self._started or self._closed:
            raise RuntimeError("实时语音会话没有启动")
        self.conversation.create_response(
            instructions=instructions,
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
        )
        self._response_requested_at = time.perf_counter()

    def cancel_response_only(self) -> None:
        if self.conversation is not None and self._started and not self._closed:
            try:
                self.conversation.cancel_response()
            except Exception:
                pass

    def response_events(self) -> Iterator[tuple[str, object]]:
        timeout = float(self.settings.get("response_timeout_seconds", 25))
        while True:
            if self._pending:
                kind, payload = self._pending.pop(0)
            else:
                try:
                    kind, payload = self.callback.events.get(timeout=timeout)
                except queue.Empty as error:
                    raise TimeoutError("实时语音回答超时") from error
            if kind == "transcript" and self._transcript_at is None:
                self._transcript_at = time.perf_counter()
            elif kind == "reply_delta":
                self._reply_parts.append(str(payload))
            elif kind == "reply_done":
                self._final_reply = str(payload).strip()
            elif kind == "audio" and self._first_audio_at is None:
                self._first_audio_at = time.perf_counter()
            elif kind == "error":
                raise RuntimeError(str(payload))
            elif kind == "done":
                self._response_done_at = time.perf_counter()
            yield kind, payload
            if kind == "done":
                return

    def cancel(self) -> None:
        if self.conversation is not None and self._started and not self._closed:
            try:
                self.conversation.cancel_response()
            except Exception:
                pass
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.conversation is not None:
            try:
                self.conversation.close()
            except Exception:
                pass
