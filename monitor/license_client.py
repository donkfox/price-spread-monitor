from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .license_trust import COMMERCIAL_CA_PEM, COMMERCIAL_SERVER_URL
from .secure_store import SecretProtectionError, protect_secret, unprotect_secret


UTC = timezone.utc
COMMERCIAL_BUILD_ENV = "PRICE_MONITOR_COMMERCIAL_BUILD"


class LicenseClientError(RuntimeError):
    pass


class LicenseRejected(LicenseClientError):
    pass


class LicenseUnavailable(LicenseClientError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _windows_machine_guid() -> str:
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except (ImportError, OSError):
        return "|".join(
            (
                platform.node(),
                socket.gethostname(),
                os.environ.get("COMPUTERNAME", ""),
            )
        )


def device_fingerprint() -> str:
    source = f"price-monitor-v1|{_windows_machine_guid()}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LicenseStatus:
    state: str
    can_scan: bool
    message: str
    expires_at: str = ""
    offline: bool = False
    license_id: int | None = None

    @property
    def display_expiry(self) -> str:
        if not self.expires_at:
            return "--"
        try:
            return _parse_time(self.expires_at).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.expires_at


class LicenseClient:
    def __init__(
        self,
        config: dict,
        root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        settings = config.get("licensing", {})
        self.commercial_build = os.environ.get(COMMERCIAL_BUILD_ENV) == "1"
        self.enabled = bool(settings.get("enabled", False)) or self.commercial_build
        self.ca_cert_data: str | None = None
        if self.commercial_build:
            self.server_url = COMMERCIAL_SERVER_URL
            self.offline_grace = timedelta(minutes=15)
            self.verify_interval = timedelta(minutes=5)
            self.timeout = 8
            configured_ca_value = ""
            self.ca_cert_data = COMMERCIAL_CA_PEM
        else:
            self.server_url = str(settings.get("server_url", "")).strip().rstrip("/")
            self.offline_grace = timedelta(
                days=max(0, int(settings.get("offline_grace_days", 3)))
            )
            self.verify_interval = timedelta(
                hours=max(1, int(settings.get("verify_interval_hours", 6)))
            )
            self.timeout = max(2, int(settings.get("timeout_seconds", 8)))
            configured_ca_value = str(settings.get("ca_cert_file", "")).strip()
        self.ca_cert_file: Path | None = None
        if configured_ca_value:
            configured_ca = Path(configured_ca_value)
            self.ca_cert_file = (
                configured_ca if configured_ca.is_absolute() else root / configured_ca
            )
        configured_state = Path(
            "runtime/license.json"
            if self.commercial_build
            else settings.get("state_file", "runtime/license.json")
        )
        self.state_file = configured_state if configured_state.is_absolute() else root / configured_state
        self.device_id = device_fingerprint()
        self.clock = clock
        self.monotonic = monotonic
        self._trusted_state: dict | None = None
        self._verified_monotonic: float | None = None
        self._expires_monotonic: float | None = None

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if state.get("device_id") != self.device_id:
            return {}
        if self.commercial_build:
            protected_token = state.pop("token_protected", "")
            if not isinstance(protected_token, str) or not protected_token:
                return {}
            try:
                state["token"] = unprotect_secret(protected_token)
            except SecretProtectionError:
                return {}
        return state

    def _save_state(self, state: dict) -> None:
        disk_state = dict(state)
        if self.commercial_build:
            token = str(disk_state.pop("token", ""))
            try:
                disk_state["token_protected"] = protect_secret(token)
            except SecretProtectionError as error:
                raise LicenseUnavailable(str(error)) from error
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(disk_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_file)

    def _post(self, path: str, payload: dict) -> dict:
        if not self.server_url.startswith(("https://", "http://")):
            raise LicenseUnavailable("授权服务器地址尚未配置")
        request = urllib.request.Request(
            f"{self.server_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "PriceMonitor/1"},
            method="POST",
        )
        try:
            context = None
            if self.server_url.startswith("https://"):
                if self.ca_cert_data:
                    context = ssl.create_default_context(cadata=self.ca_cert_data)
                elif self.ca_cert_file:
                    if not self.ca_cert_file.is_file():
                        raise LicenseUnavailable("授权服务器证书文件缺失，请重新安装软件")
                    context = ssl.create_default_context(cafile=str(self.ca_cert_file))
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=context,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except LicenseUnavailable:
            raise
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = str(body.get("detail", "授权校验失败"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                detail = "授权校验失败"
            raise LicenseRejected(detail) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LicenseUnavailable("暂时无法连接授权服务器") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LicenseUnavailable("授权服务器返回了无法识别的数据") from error
        if not result.get("ok"):
            raise LicenseRejected(str(result.get("message", "授权校验失败")))
        return result

    def _validated_server_state(self, result: dict, token: str) -> dict:
        try:
            device_id = str(result["device_id"])
            license_id = int(result["license_id"])
            expires_at = str(result["expires_at"])
            server_time = str(result["server_time"])
            expiry = _parse_time(expires_at)
            verified_at = _parse_time(server_time)
        except (KeyError, TypeError, ValueError) as error:
            raise LicenseUnavailable("授权服务器返回的数据不完整") from error
        if device_id != self.device_id or license_id < 1 or not token:
            raise LicenseRejected("授权服务器返回的设备授权不匹配")
        if expiry <= verified_at:
            raise LicenseRejected("授权已到期，请购买新激活码续费")
        if abs((self.clock().astimezone(UTC) - verified_at).total_seconds()) > 300:
            raise LicenseRejected("系统时间异常，请校准电脑时间后重试")
        return {
            "device_id": device_id,
            "license_id": license_id,
            "token": token,
            "expires_at": expires_at,
            "last_verified_at": server_time,
        }

    def _trust_state(self, state: dict) -> None:
        verified_at = _parse_time(str(state["last_verified_at"]))
        expiry = _parse_time(str(state["expires_at"]))
        now_monotonic = self.monotonic()
        self._trusted_state = dict(state)
        self._verified_monotonic = now_monotonic
        self._expires_monotonic = now_monotonic + max(
            0.0, (expiry - verified_at).total_seconds()
        )

    def _trusted_offline_available(self) -> bool:
        if (
            self._trusted_state is None
            or self._verified_monotonic is None
            or self._expires_monotonic is None
        ):
            return False
        now = self.monotonic()
        return (
            now < self._expires_monotonic
            and now - self._verified_monotonic <= self.offline_grace.total_seconds()
        )

    def activate(self, code: str) -> LicenseStatus:
        if not self.enabled:
            return self.status(online=False)
        state = self._trusted_state or self._load_state()
        result = self._post(
            "/v1/activate",
            {
                "code": code.strip(),
                "device_id": self.device_id,
                "token": state.get("token", ""),
            },
        )
        token = str(result.get("token") or state.get("token", ""))
        if not token:
            raise LicenseUnavailable("服务器没有返回授权令牌")
        state = self._validated_server_state(result, token)
        self._save_state(state)
        self._trust_state(state)
        return self.status(online=False)

    def _active_status(self, state: dict, *, offline: bool = False) -> LicenseStatus:
        message = "授权有效"
        if offline:
            message = "授权服务器暂时不可用，正在使用离线宽限期"
        return LicenseStatus(
            "offline_grace" if offline else "active",
            True,
            message,
            str(state.get("expires_at", "")),
            offline,
            int(state["license_id"]) if state.get("license_id") else None,
        )

    def status(self, *, online: bool = True, force_online: bool = False) -> LicenseStatus:
        if not self.enabled:
            return LicenseStatus("disabled", True, "当前版本未启用商业授权")
        if not self.server_url:
            return LicenseStatus("misconfigured", False, "授权服务器地址尚未配置")
        state = self._trusted_state or self._load_state()
        if not state.get("token") or not state.get("expires_at"):
            return LicenseStatus("inactive", False, "尚未激活，请输入购买后收到的激活码")

        if self._trusted_state is None and not online:
            return LicenseStatus(
                "verification_required",
                False,
                "启动监控前需要联网校验授权",
                str(state.get("expires_at", "")),
            )

        verification_due = force_online or self._trusted_state is None
        if self._verified_monotonic is not None:
            verification_due = verification_due or (
                self.monotonic() - self._verified_monotonic
                >= self.verify_interval.total_seconds()
            )
        if online and verification_due:
            try:
                result = self._post(
                    "/v1/check",
                    {"device_id": self.device_id, "token": state["token"]},
                )
                state = self._validated_server_state(result, str(state["token"]))
            except LicenseRejected as error:
                return LicenseStatus("rejected", False, str(error), str(state["expires_at"]))
            except LicenseUnavailable:
                if self._trusted_offline_available():
                    return self._active_status(self._trusted_state or state, offline=True)
                return LicenseStatus(
                    "offline_expired",
                    False,
                    "启动监控前必须联网完成授权校验",
                    str(state["expires_at"]),
                )
            self._save_state(state)
            self._trust_state(state)
        elif self._trusted_state is None:
            return LicenseStatus(
                "verification_required",
                False,
                "启动监控前需要联网校验授权",
                str(state["expires_at"]),
            )
        elif self._expires_monotonic is not None and self.monotonic() >= self._expires_monotonic:
            return LicenseStatus(
                "expired",
                False,
                "授权已到期，请购买新激活码续费",
                str(state["expires_at"]),
            )
        return self._active_status(self._trusted_state or state)

    def require_scan(self, *, force_online: bool = False) -> LicenseStatus:
        status = self.status(online=True, force_online=force_online)
        if not status.can_scan:
            raise LicenseClientError(status.message)
        return status
