from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.robot_realtime_voice import QwenRealtimeVoiceSession
from monitor.secure_store import unprotect_named_secret


CONFIG = json.loads((ROOT / "robot_config.json").read_text(encoding="utf-8-sig"))


def main() -> None:
    key_file = ROOT / str(CONFIG["dialogue"]["encrypted_key_file"])
    api_key = unprotect_named_secret(
        key_file.read_text(encoding="ascii").strip(),
        "robot-dialogue-key",
    )
    with wave.open(str(ROOT / "runtime" / "voice-benchmark.wav"), "rb") as source:
        pcm = source.readframes(source.getnframes())
    session = QwenRealtimeVoiceSession(
        CONFIG["device_gateway"]["realtime_voice"],
        lambda: api_key,
        instructions="你是小帮。用户刚刚唤醒你。请用很短的中文回答，表达你在听，不要展开。",
    )
    audio = bytearray()
    try:
        session.start()
        for offset in range(0, len(pcm), 3200):
            session.send(pcm[offset : offset + 3200])
        session.commit_input()
        session.request_response("只用一句很短的话回应：我在听。")
        for kind, payload in session.response_events():
            if kind == "audio":
                audio.extend(bytes(payload))
            if kind == "done":
                break
    finally:
        session.close()
    if not audio:
        raise RuntimeError("没有生成唤醒回应音频")
    target = ROOT / "runtime" / "wake-ack-tina.wav"
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(bytes(audio))
    print(target)


if __name__ == "__main__":
    main()
