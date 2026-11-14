from __future__ import annotations

import secrets
from pathlib import Path

from .secure_store import protect_named_secret, unprotect_named_secret


DEVICE_TOKEN_PURPOSE = "robot-device-token"
DEFAULT_DEVICE_TOKEN_FILE = "secrets/robot_device_token.dat"


def ensure_device_token(root: Path, settings: dict) -> str:
    path = root / str(
        settings.get("encrypted_device_token_file", DEFAULT_DEVICE_TOKEN_FILE)
    )
    if path.is_file():
        return unprotect_named_secret(
            path.read_text(encoding="ascii").strip(),
            DEVICE_TOKEN_PURPOSE,
        )

    token = secrets.token_urlsafe(32)
    protected = protect_named_secret(token, DEVICE_TOKEN_PURPOSE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(protected, encoding="ascii")
    temporary.replace(path)
    return token
