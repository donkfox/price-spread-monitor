from __future__ import annotations

import secrets
import shutil
import struct
import subprocess
import threading
from collections.abc import Iterable, Iterator


OPUS_CLOCK_RATE = 48000


def _ogg_crc(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte << 24
        for _ in range(8):
            value = (
                ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if value & 0x80000000
                else (value << 1) & 0xFFFFFFFF
            )
    return value


class OggOpusMuxer:
    """Wraps Xiaozhi raw Opus packets in an Ogg stream for cloud ASR."""

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int = 1,
        frame_duration_ms: int = 60,
        serial: int | None = None,
    ):
        if sample_rate <= 0 or channels != 1 or frame_duration_ms <= 0:
            raise ValueError("Opus 音频参数无效")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frame_duration_ms = int(frame_duration_ms)
        self.serial = int(serial if serial is not None else secrets.randbits(32))
        self.sequence = 0
        self.granule_position = 0

    @staticmethod
    def _lacing(packet: bytes) -> bytes:
        full_segments, remainder = divmod(len(packet), 255)
        segments = [255] * full_segments + [remainder]
        if len(segments) > 255:
            raise ValueError("单个 Opus 数据包过大")
        return bytes(segments)

    def _page(self, packet: bytes, *, flags: int = 0, granule: int = 0) -> bytes:
        lacing = self._lacing(packet)
        header = (
            b"OggS"
            + bytes((0, flags))
            + struct.pack("<QII", granule, self.serial, self.sequence)
            + b"\x00\x00\x00\x00"
            + bytes((len(lacing),))
            + lacing
        )
        self.sequence += 1
        page = header + packet
        checksum = _ogg_crc(page)
        return page[:22] + struct.pack("<I", checksum) + page[26:]

    def headers(self) -> tuple[bytes, bytes]:
        opus_head = (
            b"OpusHead"
            + bytes((1, self.channels))
            + struct.pack("<HIhB", 312, self.sample_rate, 0, 0)
        )
        vendor = b"Xiaobang"
        opus_tags = (
            b"OpusTags"
            + struct.pack("<I", len(vendor))
            + vendor
            + struct.pack("<I", 0)
        )
        return (
            self._page(opus_head, flags=0x02),
            self._page(opus_tags),
        )

    def packet(self, packet: bytes) -> bytes:
        if not packet:
            raise ValueError("Opus 数据包为空")
        self.granule_position += (
            OPUS_CLOCK_RATE * self.frame_duration_ms // 1000
        )
        return self._page(packet, granule=self.granule_position)


class OggOpusPacketParser:
    """Extracts raw Opus packets from a streaming Ogg byte sequence."""

    def __init__(self):
        self._buffer = bytearray()
        self._packet = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        if data:
            self._buffer.extend(data)
        packets: list[bytes] = []
        while True:
            marker = self._buffer.find(b"OggS")
            if marker < 0:
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                break
            if marker:
                del self._buffer[:marker]
            if len(self._buffer) < 27:
                break
            segment_count = self._buffer[26]
            header_size = 27 + segment_count
            if len(self._buffer) < header_size:
                break
            lacing = self._buffer[27:header_size]
            body_size = sum(lacing)
            page_size = header_size + body_size
            if len(self._buffer) < page_size:
                break
            body = memoryview(self._buffer)[header_size:page_size]
            offset = 0
            for length in lacing:
                self._packet.extend(body[offset:offset + length])
                offset += length
                if length < 255:
                    packet = bytes(self._packet)
                    self._packet.clear()
                    if packet and not packet.startswith((b"OpusHead", b"OpusTags")):
                        packets.append(packet)
            del body
            del self._buffer[:page_size]
        return packets


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as error:
        raise RuntimeError("没有找到 FFmpeg，无法向实体小帮发送语音") from error


def transcode_audio_to_opus_packets(
    chunks: Iterable[bytes],
    *,
    input_format: str = "mp3",
    sample_rate: int = 24000,
    frame_duration_ms: int = 60,
) -> Iterator[bytes]:
    """Streams audio through FFmpeg and yields Xiaozhi-compatible Opus packets."""

    if input_format not in {"mp3", "wav"}:
        raise ValueError("实体语音输入格式只支持 MP3 或 WAV")

    process = subprocess.Popen(
        [
            _ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            input_format,
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "libopus",
            "-application",
            "voip",
            "-frame_duration",
            str(frame_duration_ms),
            "-b:a",
            "32k",
            "-vbr",
            "off",
            "-flush_packets",
            "1",
            "-page_duration",
            str(frame_duration_ms * 1000),
            "-f",
            "ogg",
            "pipe:1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("FFmpeg 音频管道创建失败")

    writer_error: list[Exception] = []

    def write_input() -> None:
        try:
            for chunk in chunks:
                if chunk:
                    process.stdin.write(chunk)
                    process.stdin.flush()
        except (BrokenPipeError, OSError, RuntimeError) as error:
            writer_error.append(error)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=write_input, name="xiaobang-opus-input", daemon=True)
    writer.start()
    parser = OggOpusPacketParser()
    completed = False
    try:
        while chunk := process.stdout.read1(4096):
            yield from parser.feed(chunk)
        writer.join(timeout=10)
        return_code = process.wait(timeout=10)
        completed = True
        error_text = process.stderr.read().decode("utf-8", errors="replace").strip()
        if writer_error:
            raise RuntimeError(f"实体语音输入中断：{writer_error[0]}")
        if return_code != 0:
            raise RuntimeError(error_text or "实体语音 Opus 转换失败")
    finally:
        if not completed and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        writer.join(timeout=2)


def transcode_mp3_to_opus_packets(
    chunks: Iterable[bytes],
    *,
    sample_rate: int = 24000,
    frame_duration_ms: int = 60,
) -> Iterator[bytes]:
    yield from transcode_audio_to_opus_packets(
        chunks,
        input_format="mp3",
        sample_rate=sample_rate,
        frame_duration_ms=frame_duration_ms,
    )
