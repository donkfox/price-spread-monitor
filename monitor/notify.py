from __future__ import annotations

import html
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .models import Opportunity, price_trend_label


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def opportunity_notification(
    opportunity: Opportunity,
    category: str,
    level: str = "A",
) -> tuple[str, str]:
    title = f"{level}级价差机会 {opportunity.sku} 利润¥{opportunity.spread}"
    lines = [
            f"- 报警等级：{level}级",
            f"- 品类：{category}",
            f"- 商品：{opportunity.title}",
            f"- 货号：{opportunity.sku}",
            f"- 规格：{opportunity.size}",
            f"- 拼多多详情实价：¥{opportunity.pdd_price}",
            f"- 得物同规格最近成交：¥{opportunity.recent_sale_price}",
            f"- 保守试算价：¥{opportunity.evaluation_sale_price}",
            f"- 得物预计收入：¥{opportunity.estimated_income}",
            f"- 预计利润：¥{opportunity.spread}",
    ]
    profile = opportunity.sales_profile
    if profile is not None:
        lines.extend((
            f"- 7天同规格观察成交：{profile.observed_sales_7d}次",
            f"- 7天活跃成交：{profile.active_days_7d}天",
            f"- 最近成交：{profile.latest_age_hours:g}小时前",
            f"- 成交价区间：¥{profile.minimum_price}-¥{profile.maximum_price}",
            f"- 价格趋势：{price_trend_label(profile.price_trend)}",
        ))
    lines.extend((
        "",
        "请立即在拼多多搜索货号并选择上述规格，付款前复核价格和库存。",
    ))
    description = "\n".join(lines)
    return title, description


def windows_toast(title: str, message: str) -> None:
    safe_title = html.escape(title).replace("'", "&apos;")
    safe_message = html.escape(message).replace("'", "&apos;")
    script = rf"""
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{safe_title}</text><text>{safe_message}</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('价差监控').Show($toast)
"""
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


@dataclass(frozen=True)
class WeComBotConfig:
    webhook_url: str
    timeout_seconds: float = 8.0
    retries: int = 2


class WeComBotNotifier:
    def __init__(self, config: WeComBotConfig, logger: logging.Logger):
        self.config = config
        self.log = logger

    @classmethod
    def from_config(
        cls,
        config: dict,
        root: Path,
        logger: logging.Logger,
    ) -> "WeComBotNotifier | None":
        settings = config.get("notification", {}).get("wecom_bot", {})
        if not settings.get("enabled", False):
            return None
        webhook_path_value = settings.get("webhook_file")
        if not webhook_path_value:
            logger.error("企业微信群机器人已启用，但没有配置 webhook_file")
            return None
        webhook_path = Path(webhook_path_value)
        if not webhook_path.is_absolute():
            webhook_path = root / webhook_path
        try:
            webhook_url = webhook_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            logger.error("无法读取企业微信 Webhook 文件 %s：%s", webhook_path, error)
            return None
        parsed = urlparse(webhook_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "qyapi.weixin.qq.com"
            or parsed.path != "/cgi-bin/webhook/send"
            or not parsed.query.startswith("key=")
        ):
            logger.error("企业微信 Webhook 文件格式不正确：%s", webhook_path)
            return None
        return cls(
            WeComBotConfig(
                webhook_url=webhook_url,
                timeout_seconds=float(settings.get("timeout_seconds", 8)),
                retries=max(1, int(settings.get("retries", 2))),
            ),
            logger,
        )

    def _send_once(self, title: str, description: str) -> None:
        body = json.dumps(
            {
                "msgtype": "markdown",
                "markdown": {"content": f"**{title}**\n\n{description}"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.config.webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        code = payload.get("errcode")
        if code not in (0, "0"):
            raise RuntimeError(payload.get("errmsg") or f"返回码 {code}")

    def send(self, title: str, description: str) -> bool:
        for attempt in range(1, self.config.retries + 1):
            try:
                self._send_once(title, description)
                self.log.info("企业微信群机器人通知发送成功")
                return True
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
                self.log.warning(
                    "企业微信群机器人通知发送失败（%s/%s）：%s",
                    attempt,
                    self.config.retries,
                    error,
                )
                if attempt < self.config.retries:
                    time.sleep(1)
        return False

    def send_opportunity(self, opportunity: Opportunity, category: str, level: str = "A") -> bool:
        title, description = opportunity_notification(opportunity, category, level)
        return self.send(title, description)


@dataclass(frozen=True)
class PushDeerConfig:
    pushkey: str
    timeout_seconds: float = 8.0
    retries: int = 2


class PushDeerNotifier:
    def __init__(self, config: PushDeerConfig, logger: logging.Logger):
        self.config = config
        self.log = logger

    @classmethod
    def from_config(
        cls,
        config: dict,
        root: Path,
        logger: logging.Logger,
    ) -> "PushDeerNotifier | None":
        settings = config.get("notification", {}).get("pushdeer", {})
        if not settings.get("enabled", False):
            return None
        key_path_value = settings.get("pushkey_file")
        if not key_path_value:
            logger.error("PushDeer 已启用，但没有配置 pushkey_file")
            return None
        key_path = Path(key_path_value)
        if not key_path.is_absolute():
            key_path = root / key_path
        try:
            pushkey = key_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            logger.error("无法读取 PushDeer 密钥文件 %s：%s", key_path, error)
            return None
        if not pushkey.startswith("PDU"):
            logger.error("PushDeer 密钥文件格式不正确：%s", key_path)
            return None
        return cls(
            PushDeerConfig(
                pushkey=pushkey,
                timeout_seconds=float(settings.get("timeout_seconds", 8)),
                retries=max(1, int(settings.get("retries", 2))),
            ),
            logger,
        )

    def _send_once(self, title: str, description: str) -> None:
        body = urlencode(
            {
                "pushkey": self.config.pushkey,
                "text": title,
                "desp": description,
                "type": "markdown",
            }
        ).encode("utf-8")
        request = Request(
            "https://api2.pushdeer.com/message/push",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        code = payload.get("error", payload.get("code", 0))
        if code not in (0, "0", None):
            detail = payload.get("message") or payload.get("content") or f"返回码 {code}"
            raise RuntimeError(str(detail))
        result = payload.get("content", {}).get("result")
        if isinstance(result, list) and not result:
            raise RuntimeError("PushKey 未绑定任何手机设备")

    def send(self, title: str, description: str) -> bool:
        for attempt in range(1, self.config.retries + 1):
            try:
                self._send_once(title, description)
                self.log.info("PushDeer 安卓通知发送成功")
                return True
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
                self.log.warning(
                    "PushDeer 安卓通知发送失败（%s/%s）：%s",
                    attempt,
                    self.config.retries,
                    error,
                )
                if attempt < self.config.retries:
                    time.sleep(1)
        return False

    def send_opportunity(self, opportunity: Opportunity, category: str, level: str = "A") -> bool:
        title, description = opportunity_notification(opportunity, category, level)
        return self.send(title, description)


@dataclass(frozen=True)
class NtfyConfig:
    server: str
    topic: str
    priority: int = 5
    timeout_seconds: float = 8.0
    retries: int = 2
    direct_ip: str = ""


class _DirectHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        direct_ip: str,
        port: int,
        *,
        timeout: float,
    ):
        super().__init__(
            hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self.direct_ip = direct_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.direct_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class NtfyNotifier:
    def __init__(self, config: NtfyConfig, logger: logging.Logger):
        self.config = config
        self.log = logger

    @classmethod
    def from_config(
        cls,
        config: dict,
        root: Path,
        logger: logging.Logger,
    ) -> "NtfyNotifier | None":
        settings = config.get("notification", {}).get("ntfy", {})
        if not settings.get("enabled", False):
            return None
        topic_path_value = settings.get("topic_file")
        if not topic_path_value:
            logger.error("ntfy 已启用，但没有配置 topic_file")
            return None
        topic_path = Path(topic_path_value)
        if not topic_path.is_absolute():
            topic_path = root / topic_path
        try:
            topic = topic_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            logger.error("无法读取 ntfy 主题文件 %s：%s", topic_path, error)
            return None
        if len(topic) < 16 or not all(char.isalnum() or char in "-_" for char in topic):
            logger.error("ntfy 私密主题格式不正确：%s", topic_path)
            return None
        server = str(settings.get("server", "https://ntfy.sh")).rstrip("/")
        direct_ip = str(settings.get("direct_ip", "")).strip()
        if direct_ip:
            try:
                ipaddress.ip_address(direct_ip)
            except ValueError:
                logger.error("ntfy direct_ip 格式不正确：%s", direct_ip)
                return None
        return cls(
            NtfyConfig(
                server=server,
                topic=topic,
                priority=max(1, min(5, int(settings.get("priority", 5)))),
                timeout_seconds=float(settings.get("timeout_seconds", 8)),
                retries=max(1, int(settings.get("retries", 2))),
                direct_ip=direct_ip,
            ),
            logger,
        )

    def _send_direct(self, body: bytes) -> dict:
        parsed = urlparse(self.config.server)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("ntfy 直连模式要求 HTTPS 服务器地址")
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.params:
            path += f";{parsed.params}"
        if parsed.query:
            path += f"?{parsed.query}"
        connection = _DirectHTTPSConnection(
            parsed.hostname,
            self.config.direct_ip,
            port,
            timeout=self.config.timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Host": parsed.hostname,
                },
            )
            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise RuntimeError(f"ntfy HTTP {response.status}")
            return json.loads(response_body.decode("utf-8"))
        finally:
            connection.close()

    def _send_once(self, title: str, description: str) -> None:
        body = json.dumps(
            {
                "topic": self.config.topic,
                "title": title,
                "message": description,
                "priority": self.config.priority,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if self.config.direct_ip:
            payload = self._send_direct(body)
        else:
            request = Request(
                self.config.server,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        if payload.get("event") != "message" or not payload.get("id"):
            raise RuntimeError(payload.get("error") or "ntfy 未确认消息入队")

    def send(self, title: str, description: str) -> bool:
        for attempt in range(1, self.config.retries + 1):
            try:
                self._send_once(title, description)
                self.log.info("ntfy 安卓高优先级通知发送成功")
                return True
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
                self.log.warning(
                    "ntfy 安卓通知发送失败（%s/%s）：%s",
                    attempt,
                    self.config.retries,
                    error,
                )
                if attempt < self.config.retries:
                    time.sleep(1)
        return False

    def send_opportunity(self, opportunity: Opportunity, category: str, level: str = "A") -> bool:
        title, description = opportunity_notification(opportunity, category, level)
        return self.send(title, description)
