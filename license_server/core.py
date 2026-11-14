from __future__ import annotations

import csv
import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator


UTC = timezone.utc
VALID_PLAN_DAYS = frozenset({1, 30, 90, 365})


class LicenseError(RuntimeError):
    """A user-facing activation or license error."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_code(code: str) -> str:
    return "".join(character for character in code.upper() if character.isalnum())


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_code(raw: str) -> str:
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))


def new_activation_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(20))
    return format_code(raw)


@dataclass(frozen=True)
class LicenseResult:
    license_id: int
    device_id: str
    expires_at: str
    token: str | None = None


class LicenseStore:
    def __init__(self, database: Path, clock=utc_now):
        self.database = Path(database)
        self.clock = clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS licenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE TABLE IF NOT EXISTS activation_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_hash TEXT NOT NULL UNIQUE,
                    plan_days INTEGER NOT NULL,
                    batch_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    redeemed_at TEXT,
                    redeemed_device_id TEXT,
                    license_id INTEGER REFERENCES licenses(id)
                );
                CREATE INDEX IF NOT EXISTS idx_codes_redeemed_device
                    ON activation_codes(redeemed_device_id);
                """
            )

    def create_codes(self, plan_days: int, count: int, batch_name: str = "") -> list[str]:
        if plan_days not in VALID_PLAN_DAYS:
            raise ValueError("授权天数只能是 1、30、90 或 365 天")
        if count < 1 or count > 10000:
            raise ValueError("单次生成数量必须在 1 到 10000 之间")
        created_at = to_timestamp(self.clock())
        codes: list[str] = []
        with self._connect() as connection:
            for _ in range(count):
                while True:
                    code = new_activation_code()
                    try:
                        connection.execute(
                            """
                            INSERT INTO activation_codes(code_hash, plan_days, batch_name, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (hash_secret(normalize_code(code)), plan_days, batch_name.strip(), created_at),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    codes.append(code)
                    break
        return codes

    @staticmethod
    def export_codes(path: Path, codes: Iterable[str], plan_days: int) -> None:
        product_names = {
            1: "价差监控 1 天体验",
            30: "价差监控 30 天",
            90: "价差监控 90 天",
            365: "价差监控 365 天",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("activation_code", "plan_days", "product_name"))
            writer.writeheader()
            for code in codes:
                writer.writerow(
                    {
                        "activation_code": code,
                        "plan_days": plan_days,
                        "product_name": product_names[plan_days],
                    }
                )

    def redeem(self, code: str, device_id: str, current_token: str = "") -> LicenseResult:
        normalized = normalize_code(code)
        if len(normalized) != 20:
            raise LicenseError("激活码格式不正确")
        if len(device_id.strip()) < 16:
            raise LicenseError("设备编号不正确")

        now = self.clock().astimezone(UTC)
        timestamp = to_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            code_row = connection.execute(
                "SELECT * FROM activation_codes WHERE code_hash=?",
                (hash_secret(normalized),),
            ).fetchone()
            if code_row is None:
                raise LicenseError("激活码不存在")
            if code_row["redeemed_at"] is not None:
                raise LicenseError("该激活码已经使用过")

            license_row = connection.execute(
                "SELECT * FROM licenses WHERE device_id=?", (device_id.strip(),)
            ).fetchone()
            token = ""
            if license_row is None:
                token = secrets.token_urlsafe(32)
                expiry = now + timedelta(days=int(code_row["plan_days"]))
                cursor = connection.execute(
                    """
                    INSERT INTO licenses(
                        device_id, token_hash, expires_at, enabled, created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    """,
                    (device_id.strip(), hash_secret(token), to_timestamp(expiry), timestamp, timestamp, timestamp),
                )
                license_id = int(cursor.lastrowid)
            else:
                if not current_token or hash_secret(current_token) != license_row["token_hash"]:
                    raise LicenseError("该电脑已有授权，续费时需要本机原授权令牌")
                if not bool(license_row["enabled"]):
                    raise LicenseError("该授权已被停用，请联系售后")
                current_expiry = from_timestamp(license_row["expires_at"])
                expiry = max(now, current_expiry) + timedelta(days=int(code_row["plan_days"]))
                license_id = int(license_row["id"])
                connection.execute(
                    """
                    UPDATE licenses
                    SET expires_at=?, updated_at=?, last_seen_at=?
                    WHERE id=?
                    """,
                    (to_timestamp(expiry), timestamp, timestamp, license_id),
                )

            connection.execute(
                """
                UPDATE activation_codes
                SET redeemed_at=?, redeemed_device_id=?, license_id=?
                WHERE id=?
                """,
                (timestamp, device_id.strip(), license_id, int(code_row["id"])),
            )
        return LicenseResult(license_id, device_id.strip(), to_timestamp(expiry), token or None)

    def check(self, device_id: str, token: str) -> LicenseResult:
        now = self.clock().astimezone(UTC)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM licenses WHERE device_id=?", (device_id.strip(),)
            ).fetchone()
            if row is None or not token or hash_secret(token) != row["token_hash"]:
                raise LicenseError("授权令牌无效，请重新激活或联系售后")
            if not bool(row["enabled"]):
                raise LicenseError("该授权已被停用，请联系售后")
            if from_timestamp(row["expires_at"]) <= now:
                raise LicenseError("授权已到期，请购买新激活码续费")
            connection.execute(
                "UPDATE licenses SET last_seen_at=?, updated_at=? WHERE id=?",
                (to_timestamp(now), to_timestamp(now), int(row["id"])),
            )
        return LicenseResult(int(row["id"]), device_id.strip(), row["expires_at"])

    def set_enabled(self, license_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE licenses SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), to_timestamp(self.clock()), license_id),
            )
            if cursor.rowcount != 1:
                raise LicenseError("没有找到该授权")

    def extend(self, license_id: int, days: int) -> str:
        if days < 1 or days > 3650:
            raise ValueError("补偿天数必须在 1 到 3650 之间")
        now = self.clock().astimezone(UTC)
        with self._connect() as connection:
            row = connection.execute("SELECT expires_at FROM licenses WHERE id=?", (license_id,)).fetchone()
            if row is None:
                raise LicenseError("没有找到该授权")
            expiry = max(now, from_timestamp(row["expires_at"])) + timedelta(days=days)
            connection.execute(
                "UPDATE licenses SET expires_at=?, updated_at=? WHERE id=?",
                (to_timestamp(expiry), to_timestamp(now), license_id),
            )
        return to_timestamp(expiry)

    def unbind(self, license_id: int) -> tuple[str, int]:
        """Revoke one device and issue a fresh replacement code for its remaining days."""
        now = self.clock().astimezone(UTC)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
            if row is None:
                raise LicenseError("没有找到该授权")
            remaining = max(1, (from_timestamp(row["expires_at"]) - now).days + 1)
            connection.execute(
                "UPDATE licenses SET enabled=0, updated_at=? WHERE id=?",
                (to_timestamp(now), license_id),
            )
        # Replacement codes may have arbitrary remaining days after an admin-approved transfer.
        code = new_activation_code()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO activation_codes(code_hash, plan_days, batch_name, created_at)
                VALUES (?, ?, '设备解绑补发', ?)
                """,
                (hash_secret(normalize_code(code)), remaining, to_timestamp(now)),
            )
        return code, remaining

    def list_licenses(self, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, device_id, expires_at, enabled, created_at, updated_at, last_seen_at
                FROM licenses ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
