from __future__ import annotations

import asyncio
import array
import hmac
import io
import ipaddress
import json
import logging
import math
import queue
import re
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .robot_device import XiaozhiProtocolError
from .robot_noise import NOISE_TRANSCRIPTS, is_noise_transcript
from .robot_realtime_voice import QwenRealtimeVoiceSession
from .robot_streaming_asr import DashScopeStreamingSession


def take_speech_segment(
    buffer: str,
    *,
    eager: bool,
    flush: bool = False,
) -> tuple[str, str]:
    value = buffer.lstrip()
    if not value:
        return "", ""
    if flush:
        return value.strip(), ""
    strong = re.search(r"[。！？!?；;\n]", value)
    if eager:
        weak = re.search(r"[，,：:]", value)
        if weak and weak.end() >= 6 and (strong is None or weak.end() < strong.end()):
            return value[:weak.end()].strip(), value[weak.end():]
    if strong:
        return value[:strong.end()].strip(), value[strong.end():]
    if eager and len(value) >= 36:
        return value[:28].strip(), value[28:]
    return "", value


def pcm_chunks_to_wav(audio_chunks: list[bytes] | tuple[bytes, ...]) -> bytes:
    payload = b"".join(audio_chunks)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(payload)
    return buffer.getvalue()


def amplify_pcm16(payload: bytes, gain_db: float) -> bytes:
    """Apply a fixed PCM16 gain with a soft limiter at full scale."""
    if gain_db <= 0 or not payload:
        return payload
    sample_bytes = len(payload) - (len(payload) % 2)
    if sample_bytes == 0:
        return payload
    gain = 10.0 ** (float(gain_db) / 20.0)
    if abs(gain - 1.0) < 1e-9:
        return payload
    samples = array.array("h")
    samples.frombytes(payload[:sample_bytes])
    if sys.byteorder != "little":
        samples.byteswap()
    limit = 32768.0
    tanh = math.tanh
    for index, sample in enumerate(samples):
        scaled = int(round(limit * tanh(sample * gain / limit)))
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32768:
            scaled = -32768
        samples[index] = scaled
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class _PcmPacedSender:
    """Send 24 kHz PCM16 while keeping a target playback lead ahead of wall
    clock, so short network jitter cannot drain the device queue."""

    def __init__(
        self,
        connection,
        *,
        chunk_bytes: int = 2880,
        sample_rate: int = 24000,
        channels: int = 1,
        initial_buffer_ms: int = 0,
        on_chunk_sent=None,
        gain_db: float = 0.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.connection = connection
        self.chunk_bytes = max(960, int(chunk_bytes))
        self.bytes_per_second = max(1, sample_rate * channels * 2)
        self.lead_bytes = max(
            0,
            round(
                self.bytes_per_second
                * max(0.0, float(initial_buffer_ms))
                / 1000.0
            ),
        )
        self.lead_seconds = (
            self.lead_bytes / self.bytes_per_second if self.bytes_per_second else 0.0
        )
        self.on_chunk_sent = on_chunk_sent
        self.gain_db = float(gain_db)
        self.log = logger or logging.getLogger(__name__)
        self._pending = bytearray()
        self._sent_bytes = 0
        self._chunk_count = 0
        self._first_sent_at: float | None = None

    async def push(self, payload) -> None:
        if not payload:
            return
        self._pending.extend(payload)
        while len(self._pending) >= self.chunk_bytes:
            chunk = bytes(self._pending[: self.chunk_bytes])
            del self._pending[: self.chunk_bytes]
            await self._send_chunk(chunk)

    async def flush(self) -> None:
        if not self._pending:
            return
        chunk = bytes(self._pending)
        self._pending.clear()
        await self._send_chunk(chunk, paced=False)

    async def _send_chunk(self, chunk: bytes, *, paced: bool = True) -> None:
        if not chunk:
            return
        chunk = amplify_pcm16(chunk, self.gain_db)
        if not chunk:
            return
        self._chunk_count += 1
        if self._first_sent_at is None:
            self._first_sent_at = time.monotonic()
        elapsed_ms = (time.monotonic() - self._first_sent_at) * 1000
        self.log.info(
            "[pcm-send] chunk=%d bytes=%d paced=%s total=%d elapsed_ms=%.1f",
            self._chunk_count,
            len(chunk),
            paced,
            self._sent_bytes + len(chunk),
            elapsed_ms,
        )
        try:
            await self.connection.send(chunk)
        except Exception:
            self.log.exception(
                "[pcm-send] send failed at chunk=%d total=%d",
                self._chunk_count,
                self._sent_bytes,
            )
            raise
        self._sent_bytes += len(chunk)
        if self.on_chunk_sent is not None:
            self.on_chunk_sent()
        if not paced:
            return
        next_due_at = (
            self._first_sent_at
            + self._sent_bytes / self.bytes_per_second
            - self.lead_seconds
        )
        delay = next_due_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


class XiaozhiWebSocketGateway:
    def __init__(
        self,
        core,
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        path: str = "/xiaobang/v1/",
        asr_settings: dict | None = None,
        realtime_voice_settings: dict | None = None,
        api_key_provider=None,
        device_token: str = "",
        logger: logging.Logger,
    ):
        if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            raise ValueError("设备网关只能监听本机或局域网地址")
        if host == "0.0.0.0" and not str(device_token).strip():
            raise ValueError("局域网设备网关必须配置设备令牌")
        self.core = core
        self.host = host
        self.port = int(port)
        self.path = "/" + str(path).strip("/") + "/"
        self.asr_settings = asr_settings or {}
        self.realtime_voice_settings = realtime_voice_settings or {}
        self.api_key_provider = api_key_provider
        self.device_token = str(device_token).strip()
        self.log = logger
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        self._running = False
        self._physical_connections: dict[str, ServerConnection] = {}
        body = getattr(self.core, "body", None)
        if body is not None and hasattr(body, "add_listener"):
            body.add_listener(self._on_body_state)

    # Shared with the fallback transcribe route and wake-command parsing so
    # every voice entry point applies the same rules.
    _NOISE_TRANSCRIPTS = NOISE_TRANSCRIPTS

    def _ignored_listen_message(self, session, text: str) -> str:
        return json.dumps({
            "session_id": session.session_id,
            "type": "listen",
            "state": "stop",
            "ignored": True,
            "text": text,
        }, ensure_ascii=False)

    def _after_ignored_listen(self, device_id: str) -> None:
        try:
            self.core.devices.set_state(device_id, "idle")
        except Exception as error:
            self.log.warning("忽略噪声后恢复待机状态失败：%s", error)
        self._sync_body_expression("idle")

    async def _drain_device_messages(self, connection: ServerConnection) -> None:
        """Consume device frames while a turn method is busy sending audio."""
        while True:
            try:
                await connection.recv()
            except (ConnectionClosed, asyncio.CancelledError):
                return
            except Exception:
                return

    async def _with_device_drain(
        self,
        connection: ServerConnection,
        coro,
    ) -> None:
        drain = asyncio.create_task(self._drain_device_messages(connection))
        try:
            return await coro
        finally:
            drain.cancel()

    def _is_noise_transcript(self, text: str) -> bool:
        game = getattr(self.core, "guess_number", None)
        allow_short_ack = bool(
            game is not None
            and getattr(game, "active", False)
        )
        return is_noise_transcript(text, allow_short_ack=allow_short_ack)

    @staticmethod
    def _wake_command_has_content(command: str, wake_word: str = "小帮") -> bool:
        compact = re.sub(r"[\s，,。.!！?？:：;；、]+", "", str(command or ""))
        word = str(wake_word or "小帮").strip() or "小帮"
        pattern = r"\s*".join(re.escape(character) for character in word)
        while True:
            stripped = re.sub(pattern, "", compact, count=1)
            if stripped == compact:
                break
            compact = stripped
        return bool(compact)

    def _on_body_state(self, state: dict[str, object]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not self._running:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast_body_state(state), loop)

    @staticmethod
    def _body_message(state: dict[str, object]) -> str:
        return json.dumps(
            {
                "type": "body",
                "payload": {
                    "protocol_version": state.get("protocol_version", 1),
                    "sequence": state.get("sequence", 0),
                    "motion": state.get("motion", "stop"),
                    "speed": state.get("speed", 0),
                    "expression": state.get("expression", "idle"),
                    "pan_deg": state.get("pan_deg", 90),
                    "tilt_deg": state.get("tilt_deg", 90),
                    "remaining_ms": state.get("remaining_ms", 0),
                    "choreography": state.get("choreography"),
                    "idle_sway": state.get("idle_sway"),
                    "transition": state.get("transition"),
                    "behavior": state.get("behavior"),
                },
            },
            ensure_ascii=False,
        )

    def _sync_body_expression(
        self,
        expression: str,
        *,
        emotion_intensity: float = 1.0,
        follow_up: bool = False,
        exit_transition: bool = False,
    ) -> None:
        body = getattr(self.core, "body", None)
        if body is None or not hasattr(body, "command"):
            return
        try:
            body.command(
                expression=str(expression or "idle"),
                emotion_intensity=emotion_intensity,
                follow_up=follow_up,
                exit_transition=exit_transition,
            )
        except Exception as error:
            self.log.warning("身体表情同步失败：%s", error)

    def _sync_body_speaking(
        self,
        text: str,
        *,
        emotion_intensity: float = 1.0,
        follow_up: bool = False,
        exit_transition: bool = False,
    ) -> None:
        body = getattr(self.core, "body", None)
        if body is None or not hasattr(body, "command"):
            return
        length = len(str(text or "").strip())
        period_ms = max(320, min(600 - length * 3, 560))
        try:
            body.command(
                expression="speaking",
                nod_period_ms=period_ms,
                emotion_intensity=emotion_intensity,
                follow_up=follow_up,
                exit_transition=exit_transition,
            )
        except Exception as error:
            self.log.warning("身体说话表情同步失败：%s", error)

    async def _broadcast_body_state(self, state: dict[str, object]) -> None:
        message = self._body_message(state)
        failed: list[ServerConnection] = []
        for device_id, connection in tuple(self._physical_connections.items()):
            if self._physical_connections.get(device_id) is not connection:
                continue
            try:
                await connection.send(message)
            except ConnectionClosed:
                failed.append(connection)
        for device_id, connection in tuple(self._physical_connections.items()):
            if connection in failed and self._physical_connections.get(device_id) is connection:
                self._physical_connections.pop(device_id, None)

    def _record_turn_metrics(
        self,
        realtime: QwenRealtimeVoiceSession,
        *,
        route: str,
        reused_session: bool,
        audio_dispatch_ms: float | None = None,
    ) -> None:
        metrics = {
            "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "route": route,
            "reused_session": reused_session,
            **realtime.turn_metrics(),
        }
        if audio_dispatch_ms is not None:
            metrics["speech_end_to_audio_dispatch_ms"] = round(audio_dispatch_ms, 1)
        path = Path(getattr(self.core, "root", Path.cwd())) / str(
            self.realtime_voice_settings.get(
                "metrics_file",
                "runtime/robot-realtime-metrics.jsonl",
            )
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as target:
                target.write(json.dumps(metrics, ensure_ascii=False) + "\n")
        except OSError as error:
            self.log.warning("实时语音耗时记录失败：%s", error)
        self.log.info(
            "实时语音第 %s 轮（%s，长连接%s）：说完到文字 %sms，说完到首音 %sms",
            metrics.get("turn"),
            route,
            "复用" if reused_session else "新建",
            metrics.get("speech_end_to_transcript_ms"),
            metrics.get("speech_end_to_audio_dispatch_ms")
            if route == "local"
            else metrics.get("speech_end_to_first_audio_ms"),
        )

    def status(self) -> dict[str, object]:
        public_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        return {
            "running": self._running,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "url": f"ws://{public_host}:{self.port}{self.path}",
            "voice_backend": (
                "qwen-realtime"
                if bool(self.realtime_voice_settings.get("enabled", False))
                else "classic"
            ),
        }

    def start(self, timeout: float = 5) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="xiaobang-device-gateway",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("实体设备网关启动超时")
        if self._start_error is not None:
            raise RuntimeError(f"实体设备网关启动失败：{self._start_error}") from self._start_error

    def stop(self, timeout: float = 5) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as error:
            self._start_error = error
            self.log.exception("实体设备网关异常退出")
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        try:
            async with serve(
                self._handle,
                self.host,
                self.port,
                compression=None,
                ping_interval=15,
                ping_timeout=30,
                max_size=1024 * 1024,
                logger=self.log,
            ):
                self._running = True
                self._ready.set()
                self.log.info("实体设备网关已启动：ws://%s:%s%s", self.host, self.port, self.path)
                await self._stop_event.wait()
        finally:
            self._running = False
            self._ready.set()

    @staticmethod
    def _identity(connection: ServerConnection) -> tuple[str, str]:
        request = connection.request
        headers = request.headers
        query = parse_qs(urlparse(request.path).query)
        device_id = headers.get("Device-Id") or query.get("device_id", [""])[0]
        client_id = headers.get("Client-Id") or query.get("client_id", [""])[0]
        return str(device_id), str(client_id)

    def _authorized(self, connection: ServerConnection) -> bool:
        remote = connection.remote_address
        remote_host = str(remote[0]) if isinstance(remote, tuple) and remote else ""
        try:
            if ipaddress.ip_address(remote_host).is_loopback:
                return True
        except ValueError:
            pass
        supplied = str(connection.request.headers.get("Authorization", ""))
        expected = f"Bearer {self.device_token}"
        return bool(self.device_token) and hmac.compare_digest(supplied, expected)

    async def _send_error(
        self,
        connection: ServerConnection,
        message: str,
        *,
        device_id: str | None = None,
        session=None,
    ) -> None:
        if session is not None:
            try:
                await connection.send(json.dumps({
                    "session_id": session.session_id,
                    "type": "listen",
                    "state": "stop",
                }, ensure_ascii=False))
            except ConnectionClosed:
                pass
        try:
            await connection.send(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
        except ConnectionClosed:
            pass
        if device_id:
            try:
                self.core.devices.set_state(device_id, "idle")
            except Exception as error:
                self.log.warning("错误后恢复待机状态失败：%s", error)
            self._sync_body_expression("idle")

    async def _stream_physical_voice(
        self,
        connection: ServerConnection,
        device_id: str,
        text: str,
    ) -> None:
        packets = iter(self.core.speech.stream_opus(text))
        loop = asyncio.get_running_loop()
        next_send_at = loop.time()
        try:
            while True:
                packet = await asyncio.to_thread(next, packets, None)
                if packet is None:
                    break
                delay = next_send_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await connection.send(packet)
                self.core.devices.touch(device_id)
                next_send_at = max(next_send_at + 0.06, loop.time())
        finally:
            close = getattr(packets, "close", None)
            if close is not None:
                await asyncio.to_thread(close)

    async def _send_voice_segment(
        self,
        connection: ServerConnection,
        session,
        device_id: str,
        text: str,
        index: int,
        *,
        fallback_allowed: bool = False,
    ) -> None:
        if (
            session.input_format == "pcm"
            and not bool(self.realtime_voice_settings.get("allow_alternate_voice", False))
            and not fallback_allowed
        ):
            raise RuntimeError("唯一音色实时语音当前不可用")
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "tts",
            "state": "sentence_start",
            "text": text,
            "index": index,
        }, ensure_ascii=False))
        if session.input_format == "opus":
            await self._stream_physical_voice(connection, device_id, text)
        else:
            if self.core.speech.streaming_enabled:
                token = self.core.speech.prepare_stream(text)
                audio_url = f"/api/audio-stream/{token}"
            else:
                audio = self.core.speech.synthesize(text)
                audio_url = f"/api/audio/{audio.name}"
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "custom",
                "payload": {
                    "audio_url": audio_url,
                    "index": index,
                },
            }, ensure_ascii=False))

    async def _send_wake_ack_pcm(
        self,
        connection: ServerConnection,
        session,
        device_id: str,
    ) -> None:
        settings = self.realtime_voice_settings
        root = Path(getattr(self.core, "root", Path.cwd()))
        path = root / str(settings.get("wake_ack_file", "runtime/wake-ack-tina.wav"))
        if not path.is_file():
            raise RuntimeError(f"唤醒提示音文件不存在：{path}")
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 24000
            ):
                raise RuntimeError("唤醒提示音必须是 24kHz 单声道 PCM16 WAV")
            pcm = wav.readframes(wav.getnframes())
        chunk_bytes = int(self.realtime_voice_settings.get("audio_chunk_bytes", 2880))
        pacer = _PcmPacedSender(
            connection,
            chunk_bytes=chunk_bytes,
            on_chunk_sent=lambda: self.core.devices.touch(device_id),
            logger=self.log,
        )
        await pacer.push(pcm)
        await pacer.flush()

    async def _send_wake_ack(
        self,
        connection: ServerConnection,
        session,
        device_id: str,
    ) -> None:
        if not hasattr(self.core, "speech"):
            return
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "tts",
            "state": "start",
        }, ensure_ascii=False))
        self._sync_body_speaking("我在")
        if session.input_format == "pcm":
            await self._send_wake_ack_pcm(connection, session, device_id)
        else:
            await self._send_voice_segment(
                connection,
                session,
                device_id,
                "我在",
                0,
            )
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "tts",
            "state": "stop",
        }, ensure_ascii=False))
        self.core.devices.set_state(device_id, "listening")
        self._sync_body_expression("listening")

    async def _stream_chat_voice(
        self,
        connection: ServerConnection,
        session,
        device_id: str,
        message: str,
        *,
        tts_started: bool = False,
        acknowledgement_sent: bool = False,
        segment_index: int = 0,
        fallback_allowed: bool = False,
    ) -> dict[str, object]:
        events: queue.Queue[tuple[str, object]] = queue.Queue()

        def produce() -> None:
            try:
                answer = self.core.stream_chat(
                    message,
                    lambda delta: events.put(("delta", delta)),
                    speech_output=True,
                )
                events.put(("done", answer))
            except Exception as error:
                events.put(("error", error))

        producer = asyncio.create_task(asyncio.to_thread(produce))
        buffer = ""
        first_segment = True
        answer: dict[str, object] | None = None
        if not tts_started:
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "tts",
                "state": "start",
            }, ensure_ascii=False))
        try:
            while answer is None:
                try:
                    if not acknowledgement_sent and first_segment:
                        kind, payload = await asyncio.to_thread(events.get, True, 0.25)
                    else:
                        kind, payload = await asyncio.to_thread(events.get)
                except queue.Empty:
                    acknowledgement_sent = True
                    self.core.devices.set_state(device_id, "speaking")
                    self._sync_body_speaking("我想一下。")
                    await self._send_voice_segment(
                        connection,
                        session,
                        device_id,
                        "我想一下。",
                        segment_index,
                        fallback_allowed=fallback_allowed,
                    )
                    segment_index += 1
                    continue
                if kind == "error":
                    raise payload
                if kind == "delta":
                    buffer += str(payload)
                elif kind == "done":
                    answer = payload

                while True:
                    segment, buffer = take_speech_segment(
                        buffer,
                        eager=first_segment,
                        flush=answer is not None,
                    )
                    if not segment:
                        break
                    if first_segment:
                        emotion = self.core._emotion_for(message, segment)
                        await connection.send(json.dumps({
                            "session_id": session.session_id,
                            "type": "llm",
                            "emotion": emotion,
                            "text": "",
                        }, ensure_ascii=False))
                        self.core.devices.set_state(device_id, "speaking")
                        self._sync_body_speaking(segment)
                    await self._send_voice_segment(
                        connection,
                        session,
                        device_id,
                        segment,
                        segment_index,
                        fallback_allowed=fallback_allowed,
                    )
                    segment_index += 1
                    first_segment = False
            await producer
        finally:
            if not producer.done():
                producer.cancel()
        if answer is None:
            raise RuntimeError("小帮没有生成回答")
        if first_segment:
            raise RuntimeError("小帮生成了空回答")
        final_emotion = str(answer.get("emotion", "idle"))
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "llm",
            "emotion": final_emotion,
            "text": "",
        }, ensure_ascii=False))
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "tts",
            "state": "stop",
        }, ensure_ascii=False))
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "listen",
            "state": "stop",
        }, ensure_ascii=False))
        self.core.devices.set_state(device_id, "idle")
        self._sync_body_expression(str(final_emotion or "idle"))
        return answer

    def _new_realtime_voice(self) -> QwenRealtimeVoiceSession:
        if self.api_key_provider is None:
            raise XiaozhiProtocolError("实时语音尚未配置")
        return QwenRealtimeVoiceSession(
            self.realtime_voice_settings,
            self.api_key_provider,
            instructions=self.core.realtime_voice_instructions(),
        )

    @staticmethod
    def _pcm_has_speech(audio_chunks: tuple[bytes, ...], minimum_rms: float = 0.0025) -> bool:
        payload = b"".join(audio_chunks)
        if len(payload) < 3200:
            return False
        samples = array.array("h")
        samples.frombytes(payload[: len(payload) - (len(payload) % 2)])
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return False
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0
        return rms >= max(0.0005, float(minimum_rms))

    @staticmethod
    def _pcm_has_speech_vad(audio_chunks: tuple[bytes, ...]) -> bool:
        payload = b"".join(audio_chunks)
        if len(payload) < 3200:
            return False
        try:
            import numpy as np
            from faster_whisper.vad import VadOptions, get_speech_timestamps
        except Exception:
            return True
        samples = np.frombuffer(
            payload[: len(payload) - (len(payload) % 2)],
            dtype="<i2",
        ).astype(np.float32) / 32768.0
        if len(samples) < 3200:
            return False
        try:
            timestamps = get_speech_timestamps(
                samples,
                vad_options=VadOptions(
                    threshold=0.55,
                    min_speech_duration_ms=180,
                    min_silence_duration_ms=300,
                    speech_pad_ms=0,
                ),
                sampling_rate=16000,
            )
        except Exception:
            return False
        return bool(timestamps)

    def _local_transcribe(self, audio_chunks: list[bytes] | tuple[bytes, ...]) -> str:
        if not audio_chunks:
            raise RuntimeError("没有可用的本地录音")
        wav = pcm_chunks_to_wav(audio_chunks)
        try:
            return self.core.recognizer.transcribe(wav, "audio/wav")
        except Exception:
            try:
                debug_dir = Path(getattr(self.core, "root", Path.cwd())) / "runtime"
                debug_dir.mkdir(parents=True, exist_ok=True)
                target = debug_dir / f"debug-no-speech-{datetime.now().astimezone():%Y%m%d-%H%M%S-%f}.wav"
                target.write_bytes(wav)
            except OSError:
                pass
            raise

    async def _fallback_buffered_turn(
        self,
        connection: ServerConnection,
        session,
        device_id: str,
        audio_chunks: tuple[bytes, ...],
        *,
        prefer_local: bool = False,
    ) -> None:
        fallback = None
        if prefer_local:
            self.log.warning("云端流式识别已中断，直接使用本地 Whisper")
            text = await asyncio.to_thread(self._local_transcribe, audio_chunks)
        else:
            try:
                fallback = DashScopeStreamingSession(
                    self.asr_settings,
                    self.api_key_provider,
                    audio_format="pcm",
                    sample_rate=16000,
                    channels=1,
                    frame_duration_ms=60,
                )
                await asyncio.to_thread(fallback.start)
                for chunk in audio_chunks:
                    fallback.send(chunk)
                text = await asyncio.to_thread(fallback.finish)
            except Exception as cloud_error:
                self.log.warning("实时兜底云端流式识别失败，尝试本地 Whisper：%s", cloud_error)
                text = await asyncio.to_thread(self._local_transcribe, audio_chunks)
            finally:
                if fallback is not None:
                    try:
                        await asyncio.to_thread(fallback.cancel)
                    except Exception:
                        pass
        text = self.core.recognizer._normalize_transcript(text)
        if self._is_noise_transcript(text):
            self.log.info("忽略疑似环境噪声的识别文本：%s", text)
            await connection.send(self._ignored_listen_message(session, text))
            self._after_ignored_listen(device_id)
            return
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "stt",
            "text": text,
            "conversation_end": self.core.is_conversation_exit(text),
        }, ensure_ascii=False))
        await self._stream_chat_voice(
            connection,
            session,
            device_id,
            text,
            fallback_allowed=True,
        )

    async def _run_realtime_turn(
        self,
        connection: ServerConnection,
        session,
        device_id: str,
        realtime: QwenRealtimeVoiceSession,
        transcript: str | None = None,
        *,
        reused_session: bool = False,
    ) -> None:
        speech_ended_at = time.perf_counter()
        await asyncio.wait_for(
            asyncio.to_thread(realtime.commit_input),
            timeout=8.0,
        )
        text = str(transcript or "").strip()
        reply_preview = ""
        buffered_audio: list[bytes] = []
        if not text:
            # Wait for the input transcript before starting the response.
            # Starting the response first can finish before the transcript
            # arrives and make a valid utterance look like "no speech".
            text = await asyncio.to_thread(realtime.finish_input)
        if not text:
            raise ValueError("没有听清，请靠近麦克风再说一次")
        if not self._pcm_has_speech(
            realtime.buffered_audio,
            float(self.realtime_voice_settings.get("minimum_input_rms", 0.0025)),
        ):
            raise ValueError("没有检测到有效语音")
        text = self.core.recognizer._normalize_transcript(text)
        if self._is_noise_transcript(text):
            self.log.info("忽略疑似环境噪声的识别文本：%s", text)
            await connection.send(self._ignored_listen_message(session, text))
            self._after_ignored_listen(device_id)
            return
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "stt",
            "text": text,
            "conversation_end": self.core.is_conversation_exit(text),
        }, ensure_ascii=False))
        self.core.devices.touch(device_id)

        # Resolve deterministic commands locally before asking the realtime
        # model to speak. This prevents cloud-only replies from inventing
        # schemes or project progress that is not present in the monitor DB.
        local_result = await asyncio.to_thread(self.core.realtime_local_chat, text)
        if local_result is not None:
            spoken_reply = self.core.spoken_reply(text, str(local_result["reply"]))
            await asyncio.wait_for(
                asyncio.to_thread(
                    realtime.request_response,
                    (
                        "只用当前实时音色朗读下面这句已经由本地核心核实的回答。"
                        "不要改写、补充、解释或引用旧对话，只朗读原句："
                        f"{spoken_reply}"
                    ),
                ),
                timeout=8.0,
            )
            events = iter(realtime.response_events())
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "tts",
                "state": "start",
            }, ensure_ascii=False))
            self.core.devices.set_state(device_id, "speaking")
            self._sync_body_speaking(spoken_reply)
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "tts",
                "state": "sentence_start",
                "text": spoken_reply,
                "index": 0,
            }, ensure_ascii=False))
            local_audio_started = False
            chunk_bytes = int(
                self.realtime_voice_settings.get("audio_chunk_bytes", 4320)
            )
            pacer = _PcmPacedSender(
                connection,
                chunk_bytes=chunk_bytes,
                initial_buffer_ms=float(
                    self.realtime_voice_settings.get(
                        "audio_initial_buffer_ms",
                        300,
                    )
                ),
                on_chunk_sent=lambda: self.core.devices.touch(device_id),
                gain_db=float(
                    self.realtime_voice_settings.get("output_gain_db", 0.0)
                ),
                logger=self.log,
            )
            while True:
                event = await asyncio.to_thread(next, events, None)
                if event is None:
                    break
                kind, payload = event
                if kind == "audio":
                    local_audio_started = True
                    await pacer.push(bytes(payload))
                elif kind == "done":
                    break
            await pacer.flush()
            if not local_audio_started:
                raise RuntimeError("实时语音没有返回本地核实回答的音频")
            self._record_turn_metrics(
                realtime,
                route="local-realtime-voice",
                reused_session=reused_session,
                audio_dispatch_ms=(time.perf_counter() - speech_ended_at) * 1000,
            )
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "llm",
                "emotion": str(local_result.get("emotion", "idle")),
                "text": "",
            }, ensure_ascii=False))
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "tts",
                "state": "stop",
            }, ensure_ascii=False))
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "listen",
                "state": "stop",
            }, ensure_ascii=False))
            self.core.devices.set_state(device_id, "idle")
            self._sync_body_expression(str(local_result.get("emotion", "idle")))
            return

        await asyncio.wait_for(
            asyncio.to_thread(realtime.request_response, None),
            timeout=8.0,
        )
        events = iter(realtime.response_events())
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "tts",
            "state": "start",
        }, ensure_ascii=False))
        self.core.devices.set_state(device_id, "speaking")
        self._sync_body_speaking(reply_preview or text)

        audio_started = False
        chunk_bytes = int(
            self.realtime_voice_settings.get("audio_chunk_bytes", 4320)
        )
        pacer = _PcmPacedSender(
            connection,
            chunk_bytes=chunk_bytes,
            initial_buffer_ms=float(
                self.realtime_voice_settings.get(
                    "audio_initial_buffer_ms",
                    300,
                )
            ),
            on_chunk_sent=lambda: self.core.devices.touch(device_id),
            gain_db=float(
                self.realtime_voice_settings.get("output_gain_db", 0.0)
            ),
            logger=self.log,
        )
        if buffered_audio:
            audio_started = True
            await connection.send(json.dumps({
                "session_id": session.session_id,
                "type": "tts",
                "state": "sentence_start",
                "text": reply_preview.strip() or "小帮正在回答",
                "index": 0,
            }, ensure_ascii=False))
            for chunk in buffered_audio:
                await pacer.push(chunk)
        while True:
            event = await asyncio.to_thread(next, events, None)
            if event is None:
                break
            kind, payload = event
            if kind == "reply_delta":
                reply_preview += str(payload)
            elif kind == "audio":
                if not audio_started:
                    audio_started = True
                    await connection.send(json.dumps({
                        "session_id": session.session_id,
                        "type": "tts",
                        "state": "sentence_start",
                        "text": reply_preview.strip() or "小帮正在回答",
                        "index": 0,
                    }, ensure_ascii=False))
                await pacer.push(bytes(payload))
            elif kind == "done":
                break
        await pacer.flush()

        reply = realtime.reply
        self._record_turn_metrics(
            realtime,
            route="cloud",
            reused_session=reused_session,
        )
        if not audio_started or not reply:
            raise RuntimeError("实时语音没有返回完整回答")
        result = local_result
        if result is None:
            result = await asyncio.to_thread(self.core.finish_realtime_chat, text, reply)
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "llm",
            "emotion": str(result.get("emotion", "idle")),
            "text": "",
        }, ensure_ascii=False))
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "tts",
            "state": "stop",
        }, ensure_ascii=False))
        await connection.send(json.dumps({
            "session_id": session.session_id,
            "type": "listen",
            "state": "stop",
        }, ensure_ascii=False))
        self.core.devices.set_state(device_id, "idle")
        self._sync_body_expression(str(result.get("emotion", "idle")))

    async def _handle(self, connection: ServerConnection) -> None:
        request_path = urlparse(connection.request.path).path
        if request_path.rstrip("/") != self.path.rstrip("/"):
            await connection.close(code=1008, reason="unsupported path")
            return
        if not self._authorized(connection):
            await connection.close(code=1008, reason="device authentication required")
            return
        device_id, client_id = self._identity(connection)
        session = None
        registered = False
        streaming_asr = None
        realtime_voice = None
        realtime_turn_active = False
        realtime_turn_reused = False
        realtime_failed_audio: list[bytes] = []
        classic_pcm_audio: list[bytes] = []
        listen_started_at = 0.0
        last_speech_at = 0.0
        conversation_timeout = max(
            1.0,
            min(float(self.realtime_voice_settings.get("conversation_timeout_seconds", 10)), 60.0),
        )
        endpoint_silence = max(
            0.2,
            min(
                float(self.realtime_voice_settings.get("endpoint_silence_ms", 450)) / 1000.0,
                3.0,
            ),
        )
        wake_window: list[bytes] = []
        server_audio_tail: list[bytes] = []
        server_audio_tail_bytes = 0
        server_speech_seen = False
        server_last_vad_check_at = 0.0
        server_last_timeout_check_at = 0.0
        wake_checking = False
        last_wake_check_at = 0.0
        pending_transcript: str | None = None

        async def start_listening(
            session,
            *,
            initial_audio: tuple[bytes, ...] = (),
            initial_transcript: str | None = None,
            prime_endpoint: bool = True,
        ) -> None:
            nonlocal listen_started_at, last_speech_at
            nonlocal realtime_voice, realtime_turn_active, realtime_turn_reused
            nonlocal realtime_failed_audio, classic_pcm_audio, streaming_asr
            nonlocal server_audio_tail_bytes, server_speech_seen
            nonlocal server_last_vad_check_at, server_last_timeout_check_at
            nonlocal pending_transcript

            wake_window.clear()
            server_audio_tail.clear()
            initial_audio = tuple(initial_audio or ())
            if prime_endpoint:
                server_audio_tail.extend(initial_audio)
            server_audio_tail_bytes = sum(len(chunk) for chunk in server_audio_tail)
            server_speech_seen = False
            server_last_vad_check_at = 0.0
            server_last_timeout_check_at = 0.0
            listen_started_at = time.monotonic()
            last_speech_at = 0.0
            pending_transcript = str(initial_transcript or "").strip() or None
            if prime_endpoint and initial_audio and self._pcm_has_speech_vad(initial_audio):
                server_speech_seen = True
                last_speech_at = listen_started_at
            if self.api_key_provider is None:
                raise XiaozhiProtocolError("流式语音识别尚未配置")
            if streaming_asr is not None:
                try:
                    await asyncio.to_thread(streaming_asr.cancel)
                except Exception:
                    pass
                streaming_asr = None
            use_realtime = (
                session.input_format == "pcm"
                and bool(self.realtime_voice_settings.get("enabled", False))
            )
            if use_realtime:
                try:
                    realtime_turn_reused = bool(
                        realtime_voice is not None and realtime_voice.ready
                    )
                    if not realtime_turn_reused:
                        if realtime_voice is not None:
                            await asyncio.to_thread(realtime_voice.close)
                        realtime_voice = self._new_realtime_voice()
                        connect_timeout = max(
                            3.0,
                            float(
                                self.realtime_voice_settings.get(
                                    "connect_timeout_seconds",
                                    6,
                                )
                            )
                            + 3.0,
                        )
                        await asyncio.wait_for(
                            asyncio.to_thread(realtime_voice.start),
                            timeout=connect_timeout,
                        )
                    await asyncio.to_thread(realtime_voice.begin_turn)
                    for chunk in initial_audio:
                        realtime_voice.send(chunk)
                    realtime_turn_active = True
                    realtime_failed_audio = []
                except Exception as error:
                    if realtime_voice is not None:
                        await asyncio.to_thread(realtime_voice.close)
                    realtime_voice = None
                    realtime_turn_active = False
                    self.log.warning(
                        "实时语音连接失败，当前轮次改用经典通道：%s",
                        error,
                    )
            if realtime_voice is None:
                classic_pcm_audio = []
                try:
                    streaming_asr = DashScopeStreamingSession(
                        self.asr_settings,
                        self.api_key_provider,
                        audio_format=session.input_format,
                        sample_rate=16000,
                        channels=1,
                        frame_duration_ms=60,
                    )
                    await asyncio.to_thread(streaming_asr.start)
                    classic_pcm_audio.extend(initial_audio)
                    for chunk in initial_audio:
                        streaming_asr.send(chunk)
                except Exception as error:
                    streaming_asr = None
                    self.core.devices.set_state(device_id, "idle")
                    self.log.warning("流式语音识别连接失败，本轮继续保持设备在线：%s", error)
                    await self._send_error(
                        connection,
                        "语音识别连接失败，请再说一次",
                        device_id=device_id,
                        session=session,
                    )

        try:
            first = await asyncio.wait_for(connection.recv(), timeout=10)
            if not isinstance(first, str):
                raise XiaozhiProtocolError("设备必须先发送 hello")
            payload = json.loads(first)
            if not isinstance(payload, dict):
                raise XiaozhiProtocolError("设备消息必须是对象")
            response = self.core.devices.hello(
                payload,
                device_id=device_id,
                client_id=client_id,
            )
            registered = True
            await connection.send(json.dumps(response, ensure_ascii=False))
            session = self.core.devices.event(device_id, {
                "session_id": response["session_id"],
                "type": "heartbeat",
            })
            if not session.web_client:
                previous = self._physical_connections.get(device_id)
                if previous is not None and previous is not connection:
                    self._physical_connections[device_id] = connection
                    try:
                        await previous.close(code=1013, reason="device replaced")
                    except Exception:
                        pass
                else:
                    self._physical_connections[device_id] = connection
                body = getattr(self.core, "body", None)
                if body is not None:
                    await connection.send(self._body_message(body.status()))

            while True:
                try:
                    message = await asyncio.wait_for(connection.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if listen_started_at and time.monotonic() - max(
                        listen_started_at,
                        last_speech_at or listen_started_at,
                    ) >= conversation_timeout:
                        listen_started_at = 0.0
                        last_speech_at = 0.0
                        wake_window.clear()
                        if realtime_voice is not None:
                            await asyncio.to_thread(realtime_voice.cancel_response_only)
                            realtime_turn_active = False
                        if streaming_asr is not None:
                            await asyncio.to_thread(streaming_asr.cancel)
                            streaming_asr = None
                        self.core.devices.set_state(device_id, "idle")
                        await self._send_error(
                            connection,
                            "长时间没有听到声音，已停止监听",
                            device_id=device_id,
                            session=session,
                        )
                    continue
                if isinstance(message, bytes):
                    self.core.devices.touch(device_id)
                    if listen_started_at:
                        if session.input_format != "pcm":
                            last_speech_at = time.monotonic()
                        else:
                            server_audio_tail.append(message)
                            server_audio_tail_bytes += len(message)
                            while server_audio_tail and server_audio_tail_bytes > 9600:
                                server_audio_tail_bytes -= len(server_audio_tail.pop(0))
                            now = time.monotonic()
                            if now - server_last_vad_check_at >= 0.15:
                                server_last_vad_check_at = now
                                if self._pcm_has_speech_vad(tuple(server_audio_tail)):
                                    last_speech_at = now
                                    server_speech_seen = True
                            if now - server_last_timeout_check_at >= 0.5:
                                server_last_timeout_check_at = now
                                if now - max(
                                    listen_started_at,
                                    last_speech_at or listen_started_at,
                                ) >= conversation_timeout:
                                    listen_started_at = 0.0
                                    last_speech_at = 0.0
                                    server_speech_seen = False
                                    wake_window.clear()
                                    if realtime_voice is not None:
                                        await asyncio.to_thread(
                                            realtime_voice.cancel_response_only
                                        )
                                        realtime_turn_active = False
                                    if streaming_asr is not None:
                                        await asyncio.to_thread(streaming_asr.cancel)
                                        streaming_asr = None
                                    self.core.devices.set_state(device_id, "idle")
                                    await self._send_error(
                                        connection,
                                        "长时间没有听到声音，已停止监听",
                                        device_id=device_id,
                                        session=session,
                                    )
                                    continue
                    if (
                        listen_started_at
                        and realtime_turn_active
                        and server_speech_seen
                        and time.monotonic() - last_speech_at >= endpoint_silence
                    ):
                        self.log.info(
                            "服务端静音结束点触发：%.0f ms",
                            (time.monotonic() - last_speech_at) * 1000.0,
                        )
                        listen_started_at = 0.0
                        last_speech_at = 0.0
                        server_speech_seen = False
                        wake_window.clear()
                        active_realtime = realtime_voice
                        realtime_turn_active = False
                        buffered_audio = active_realtime.buffered_audio
                        try:
                            await self._with_device_drain(
                                connection,
                                self._run_realtime_turn(
                                    connection,
                                    session,
                                    device_id,
                                    active_realtime,
                                    transcript=pending_transcript or None,
                                    reused_session=realtime_turn_reused,
                                ),
                            )
                        except Exception as error:
                            self.log.warning(
                                "服务端结束点实时语音失败，改用备用识别：%s",
                                error,
                            )
                            await asyncio.to_thread(active_realtime.close)
                            realtime_voice = None
                            try:
                                await self._with_device_drain(
                                    connection,
                                    self._fallback_buffered_turn(
                                        connection,
                                        session,
                                        device_id,
                                        buffered_audio,
                                    ),
                                )
                            except Exception as fallback_error:
                                self.core.devices.set_state(device_id, "idle")
                                self.log.warning(
                                    "服务端结束点实时与备用识别均失败：%s",
                                    fallback_error,
                                )
                                await self._send_error(
                                    connection,
                                    "没有听清，请再说一次",
                                    device_id=device_id,
                                    session=session,
                                )
                        continue
                    recognizer = getattr(self.core, "recognizer", None)
                    if (
                        session.input_format == "pcm"
                        and not listen_started_at
                        and not realtime_turn_active
                        and recognizer is not None
                        and hasattr(recognizer, "detect_wake")
                    ):
                        wake_window.append(message)
                        wake_total = sum(len(chunk) for chunk in wake_window)
                        while wake_total > 64000 and wake_window:
                            wake_total -= len(wake_window.pop(0))
                        now = time.monotonic()
                        wake_interval = max(
                            0.5,
                            float(
                                self.realtime_voice_settings.get(
                                    "wake_check_interval_seconds",
                                    1.5,
                                )
                            ),
                        )
                        wake_min_bytes = max(
                            9600,
                            int(
                                self.realtime_voice_settings.get(
                                    "wake_min_audio_bytes",
                                    24000,
                                )
                            ),
                        )
                        wake_due = (
                            wake_total >= wake_min_bytes
                            and not wake_checking
                            and now - last_wake_check_at >= wake_interval
                        )
                        if wake_due:
                            last_wake_check_at = now
                        if (
                            wake_due
                            and self._pcm_has_speech(
                                tuple(wake_window),
                                float(self.realtime_voice_settings.get("wake_input_rms", 0.006)),
                            )
                            and (
                                not bool(
                                    self.realtime_voice_settings.get(
                                        "wake_vad_enabled",
                                        True,
                                    )
                                )
                                or self._pcm_has_speech_vad(tuple(wake_window))
                            )
                        ):
                            wake_checking = True
                            try:
                                wav = pcm_chunks_to_wav(tuple(wake_window))
                                payload = b"".join(wake_window)
                                if bool(
                                    self.realtime_voice_settings.get(
                                        "wake_debug_enabled",
                                        False,
                                    )
                                ):
                                    debug_samples = array.array("h")
                                    debug_samples.frombytes(
                                        payload[: len(payload) - (len(payload) % 2)]
                                    )
                                    if sys.byteorder != "little":
                                        debug_samples.byteswap()
                                    debug_rms = 0.0
                                    if debug_samples:
                                        debug_rms = (
                                            math.sqrt(
                                                sum(
                                                    sample * sample
                                                    for sample in debug_samples
                                                )
                                                / len(debug_samples)
                                            )
                                            / 32768.0
                                        )
                                    debug_wav = (
                                        Path(getattr(self.core, "root", Path.cwd()))
                                        / "runtime"
                                        / "debug-wake-last.wav"
                                    )
                                    try:
                                        debug_wav.parent.mkdir(parents=True, exist_ok=True)
                                        debug_wav.write_bytes(wav)
                                    except OSError:
                                        pass
                                    self.log.info(
                                        "wake debug: %.0f ms, %.1f dBFS, saved %s",
                                        len(payload) / 32.0,
                                        20.0 * math.log10(max(debug_rms, 1e-9)),
                                        debug_wav,
                                    )
                                result = await asyncio.to_thread(
                                    recognizer.detect_wake,
                                    wav,
                                    "audio/wav",
                                )
                            except Exception as error:
                                self.log.warning("本地唤醒检测失败：%s", error)
                                result = {}
                            finally:
                                wake_checking = False
                            try:
                                candidate_text = result.get("text", "")
                                if re.search(r"小\s*帮", str(candidate_text or "")):
                                    candidate_wav = (
                                        Path(getattr(self.core, "root", Path.cwd()))
                                        / "runtime"
                                        / "voiceprint-enroll-last.wav"
                                    )
                                    candidate_wav.parent.mkdir(parents=True, exist_ok=True)
                                    candidate_wav.write_bytes(wav)
                                    voiceprint = result.get("voiceprint") or {}
                                    candidate_wav.with_suffix(".json").write_text(
                                        json.dumps(
                                            {
                                                "created_at": datetime.now()
                                                .astimezone()
                                                .isoformat(timespec="seconds"),
                                                "duration_ms": len(payload) / 32.0,
                                                "text": str(candidate_text),
                                                "wake_matched": bool(
                                                    result.get("matched")
                                                ),
                                                "voiceprint_verified": bool(
                                                    voiceprint.get("verified")
                                                ),
                                                "voiceprint_score": voiceprint.get(
                                                    "score"
                                                ),
                                                "voiceprint_reason": voiceprint.get(
                                                    "reason", ""
                                                ),
                                            },
                                            ensure_ascii=False,
                                            indent=2,
                                        ),
                                        encoding="utf-8",
                                    )
                            except OSError:
                                pass
                            if result.get("matched"):
                                wake_command = str(result.get("command") or "").strip()
                                wake_transcript = str(result.get("text") or "").strip() or None
                                wake_has_content = self._wake_command_has_content(
                                    wake_command or wake_transcript
                                )
                                wake_audio = tuple(wake_window)
                                wake_window.clear()
                                await connection.send(json.dumps({
                                    "session_id": session.session_id,
                                    "type": "llm",
                                    "emotion": "happy",
                                    "text": "我在",
                                    "max_start_delay_ms": 1000,
                                }, ensure_ascii=False))
                                try:
                                    self.core.devices.set_state(device_id, "listening")
                                except Exception as error:
                                    self.log.warning("wake state update failed: %s", error)
                                self._sync_body_expression("listening")
                                try:
                                    await connection.send(json.dumps({
                                        "session_id": session.session_id,
                                        "type": "listen",
                                        "state": "start",
                                    }, ensure_ascii=False))
                                except Exception as error:
                                    self.log.warning("wake listen start failed: %s", error)
                                try:
                                    await start_listening(
                                        session,
                                        initial_audio=wake_audio,
                                        initial_transcript=(
                                            wake_transcript if wake_has_content else None
                                        ),
                                        prime_endpoint=wake_has_content,
                                    )
                                except Exception as error:
                                    self.log.warning("wake listening failed: %s", error)
                                    try:
                                        self.core.devices.set_state(device_id, "idle")
                                    except Exception as state_error:
                                        self.log.warning(
                                            "wake failure state reset failed: %s",
                                            state_error,
                                        )
                                    self._sync_body_expression("idle")
                                continue
                    if realtime_voice is not None and realtime_turn_active:
                        realtime_failed_audio.append(message)
                        try:
                            realtime_voice.send(message)
                        except Exception as error:
                            self.log.warning("实时语音连接中断，本轮改用备用识别：%s", error)
                            await asyncio.to_thread(realtime_voice.close)
                            realtime_voice = None
                            realtime_turn_active = False
                    if streaming_asr is not None:
                        if session.input_format == "pcm":
                            classic_pcm_audio.append(message)
                        try:
                            streaming_asr.send(message)
                        except Exception as error:
                            self.log.warning("云端流式识别连接中断，本轮改用本地识别：%s", error)
                            try:
                                await asyncio.to_thread(streaming_asr.cancel)
                            except Exception:
                                pass
                            streaming_asr = None
                    continue
                event = json.loads(message)
                if not isinstance(event, dict):
                    raise XiaozhiProtocolError("设备消息必须是对象")
                session = self.core.devices.event(device_id, event)
                if event.get("type") == "listen":
                    listen_state = event.get("state")
                    if listen_state == "detect":
                        await connection.send(
                            json.dumps(
                                {
                                    "session_id": session.session_id,
                                    "type": "llm",
                                    "emotion": "happy",
                                    "text": "我在",
                                    "local_audio": "wake_ack",
                                    "max_start_delay_ms": 1000,
                                },
                                ensure_ascii=False,
                            )
                        )
                        if not session.local_wake_ack and hasattr(self.core, "speech"):
                            await self._with_device_drain(
                                connection,
                                self._send_wake_ack(
                                    connection,
                                    session,
                                    device_id,
                                ),
                            )
                        try:
                            await connection.send(json.dumps({
                                "session_id": session.session_id,
                                "type": "listen",
                                "state": "start",
                            }, ensure_ascii=False))
                        except Exception as error:
                            self.log.warning("唤醒后启动实体监听失败：%s", error)
                        try:
                            await start_listening(session)
                        except Exception as error:
                            self.log.warning("唤醒后开始监听失败：%s", error)
                    elif listen_state == "start":
                        await start_listening(session)
                    elif (
                        listen_state == "stop"
                        and realtime_voice is not None
                        and realtime_turn_active
                    ):
                        listen_started_at = 0.0
                        last_speech_at = 0.0
                        active_realtime = realtime_voice
                        realtime_turn_active = False
                        buffered_audio = active_realtime.buffered_audio
                        try:
                            await self._with_device_drain(
                                connection,
                                self._run_realtime_turn(
                                    connection,
                                    session,
                                    device_id,
                                    active_realtime,
                                    transcript=pending_transcript or None,
                                    reused_session=realtime_turn_reused,
                                ),
                            )
                        except Exception as error:
                            self.log.warning(
                                "实时语音本轮失败，改用经典通道重试：%s",
                                error,
                            )
                            await asyncio.to_thread(active_realtime.close)
                            realtime_voice = None
                            try:
                                await self._with_device_drain(
                                    connection,
                                    self._fallback_buffered_turn(
                                        connection,
                                        session,
                                        device_id,
                                        buffered_audio,
                                    ),
                                )
                            except Exception as fallback_error:
                                self.core.devices.set_state(device_id, "idle")
                                self.log.warning(
                                    "实时与备用识别均失败，本轮继续保持设备在线：%s",
                                    fallback_error,
                                )
                                await self._send_error(
                                    connection,
                                    "没有听清，请再说一次",
                                    device_id=device_id,
                                    session=session,
                                )
                    elif listen_state == "stop" and realtime_failed_audio:
                        listen_started_at = 0.0
                        last_speech_at = 0.0
                        buffered_audio = tuple(realtime_failed_audio)
                        realtime_failed_audio = []
                        try:
                            await self._with_device_drain(
                                connection,
                                self._fallback_buffered_turn(
                                    connection,
                                    session,
                                    device_id,
                                    buffered_audio,
                                ),
                            )
                        except Exception as error:
                            self.core.devices.set_state(device_id, "idle")
                            self.log.warning("实时连接中断后的备用识别失败：%s", error)
                            await self._send_error(
                                connection,
                                "实时识别连接中断，请再说一次",
                                device_id=device_id,
                                session=session,
                            )
                    elif listen_state == "stop" and streaming_asr is not None:
                        listen_started_at = 0.0
                        last_speech_at = 0.0
                        active_asr = streaming_asr
                        streaming_asr = None
                        buffered_pcm = list(classic_pcm_audio)
                        classic_pcm_audio = []
                        try:
                            finish_task = asyncio.create_task(
                                asyncio.to_thread(active_asr.finish)
                            )
                            await connection.send(json.dumps({
                                "session_id": session.session_id,
                                "type": "tts",
                                "state": "start",
                            }, ensure_ascii=False))
                            self.core.devices.set_state(device_id, "speaking")
                            try:
                                text = await finish_task
                            except Exception as cloud_error:
                                self.log.warning(
                                    "云端流式识别失败，尝试本地 Whisper：%s",
                                    cloud_error,
                                )
                                text = await asyncio.to_thread(
                                    self._local_transcribe,
                                    buffered_pcm,
                                )
                            text = self.core.recognizer._normalize_transcript(text)
                            if self._is_noise_transcript(text):
                                self.log.info("忽略疑似环境噪声的识别文本：%s", text)
                                await connection.send(self._ignored_listen_message(session, text))
                                await connection.send(json.dumps({
                                    "session_id": session.session_id,
                                    "type": "tts",
                                    "state": "stop",
                                }, ensure_ascii=False))
                                self._after_ignored_listen(device_id)
                            else:
                                await connection.send(json.dumps({
                                    "session_id": session.session_id,
                                    "type": "stt",
                                    "text": text,
                                    "conversation_end": self.core.is_conversation_exit(text),
                                }, ensure_ascii=False))
                                self.core.devices.touch(device_id)
                                await self._with_device_drain(
                                    connection,
                                    self._stream_chat_voice(
                                        connection,
                                        session,
                                        device_id,
                                        text,
                                        tts_started=True,
                                        acknowledgement_sent=True,
                                        segment_index=1,
                                        fallback_allowed=True,
                                    ),
                                )
                        except Exception as error:
                            self.core.devices.set_state(device_id, "idle")
                            self.log.warning("备用识别失败，本轮继续保持设备在线：%s", error)
                            await connection.send(json.dumps({
                                "session_id": session.session_id,
                                "type": "tts",
                                "state": "stop",
                            }, ensure_ascii=False))
                            await self._send_error(
                                connection,
                                "没有听清，请再说一次",
                                device_id=device_id,
                                session=session,
                            )
                    elif listen_state == "stop" and classic_pcm_audio:
                        listen_started_at = 0.0
                        last_speech_at = 0.0
                        buffered_pcm = tuple(classic_pcm_audio)
                        classic_pcm_audio = []
                        try:
                            await self._with_device_drain(
                                connection,
                                self._fallback_buffered_turn(
                                    connection,
                                    session,
                                    device_id,
                                    buffered_pcm,
                                    prefer_local=True,
                                ),
                            )
                        except Exception as error:
                            self.core.devices.set_state(device_id, "idle")
                            self.log.warning("本地识别兜底失败：%s", error)
                            await self._send_error(
                                connection,
                                "没有听清，请再说一次",
                                device_id=device_id,
                                session=session,
                            )
                elif event.get("type") == "abort":
                    listen_started_at = 0.0
                    last_speech_at = 0.0
                    wake_window.clear()
                    self.core.devices.set_state(device_id, "idle")
                    self._sync_body_expression("idle")
                    if realtime_voice is not None:
                        await asyncio.to_thread(realtime_voice.cancel_response_only)
                        realtime_turn_active = False
                    if streaming_asr is not None:
                        await asyncio.to_thread(streaming_asr.cancel)
                        streaming_asr = None
                    classic_pcm_audio = []
        except (
            asyncio.TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
            ValueError,
            XiaozhiProtocolError,
        ) as error:
            self.log.warning("实体设备连接被拒绝：%s", error)
            await self._send_error(
                connection,
                str(error),
                device_id=device_id,
            )
            await connection.close(code=1008, reason="invalid device message")
        except ConnectionClosed:
            pass
        finally:
            if realtime_voice is not None:
                await asyncio.to_thread(realtime_voice.cancel)
            if streaming_asr is not None:
                await asyncio.to_thread(streaming_asr.cancel)
            classic_pcm_audio = []
            if registered:
                current = self._physical_connections.get(device_id)
                was_current_physical = current is connection
                if was_current_physical:
                    self._physical_connections.pop(device_id, None)
                    self.core.devices.disconnect(device_id)
                    if not self._physical_connections:
                        body = getattr(self.core, "body", None)
                        if body is not None:
                            body.command(motion="stop")
                elif bool(session and session.web_client) and current is None:
                    self.core.devices.disconnect(device_id)
