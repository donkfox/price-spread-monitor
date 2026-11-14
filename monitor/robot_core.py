from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import threading
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlparse

import requests

from .app_data import AppRepository
from .computer_access import ComputerAccessManager
from .desktop import TaskController, application_root
from .robot_body import RobotBodyController
from .robot_companion import (
    CompanionManager,
    GuessNumberGame,
    PatrolController,
    RockPaperScissors,
)
from .robot_device import XiaozhiDeviceRegistry
from .robot_gateway import XiaozhiWebSocketGateway
from .robot_mdns import DEFAULT_MDNS_HOSTNAME, RobotMdnsResponder
from .robot_noise import is_noise_transcript
from .robot_pairing import ensure_device_token
from .robot_voice import SpeechRecognizer, SpeechSynthesizer
from .secure_store import (
    SecretProtectionError,
    protect_named_secret,
    unprotect_named_secret,
)


ROBOT_PROTOCOL_VERSION = 5
PRICE_MONITOR_SKILL_VERSION = 1
DEFAULT_PROFILE = {
    "name": "未命名助手",
    "role": "长期得力助手",
    "personality": "可靠、直接、温和、有分寸",
    "owner_name": "主人",
    "mission": "真真切切地帮助人们解决工作和生活里的实际问题，让帮助能够执行、能够落地",
}
MEMORY_CATEGORIES = {
    "explicit": "主动记住",
    "preference": "偏好",
    "habit": "习惯",
    "project": "项目",
    "experience": "经历",
    "relationship": "关系",
    "identity": "身份",
}
DIALOGUE_PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-5-mini",
    },
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "default_model": "deepseek-v4-flash",
    },
    "qwen": {
        "label": "通义千问",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "default_model": "qwen3.7-flash",
        "allow_custom_endpoint": True,
    },
    "custom": {
        "label": "兼容接口",
        "endpoint": "",
        "default_model": "",
    },
}


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def _now() -> str:
    return _beijing_now().isoformat(timespec="seconds")


class RobotRepository:
    def __init__(self, database: Path, backup_dir: Path):
        self.database = database
        self.backup_dir = backup_dir
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._backup_existing_database()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _backup_existing_database(self) -> None:
        if not self.database.is_file() or self.database.stat().st_size == 0:
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        backup = self.backup_dir / f"robot-{datetime.now().astimezone():%Y-%m-%d}.sqlite3"
        if backup.exists():
            return
        source = sqlite3.connect(self.database)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    def _backup_latest(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        latest = self.backup_dir / "robot-latest.sqlite3"
        temporary = self.backup_dir / "robot-latest.tmp.sqlite3"
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(self.database)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        temporary.replace(latest)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    category TEXT NOT NULL DEFAULT 'explicit',
                    importance INTEGER NOT NULL DEFAULT 5,
                    source TEXT NOT NULL DEFAULT 'explicit',
                    confirmed INTEGER NOT NULL DEFAULT 1,
                    normalized TEXT NOT NULL DEFAULT '',
                    mention_count INTEGER NOT NULL DEFAULT 1,
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL
                );
                """
            )
            memory_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            migrations = {
                "category": "TEXT NOT NULL DEFAULT 'explicit'",
                "importance": "INTEGER NOT NULL DEFAULT 5",
                "source": "TEXT NOT NULL DEFAULT 'explicit'",
                "confirmed": "INTEGER NOT NULL DEFAULT 1",
                "normalized": "TEXT NOT NULL DEFAULT ''",
                "mention_count": "INTEGER NOT NULL DEFAULT 1",
                "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in memory_columns:
                    connection.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")
            rows = connection.execute(
                "SELECT id, content, created_at FROM memories WHERE normalized='' OR last_seen_at='' OR updated_at=''"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE memories SET normalized=?, last_seen_at=?, updated_at=? WHERE id=?",
                    (
                        self.normalize_memory(str(row["content"])),
                        str(row["created_at"]),
                        str(row["created_at"]),
                        int(row["id"]),
                    ),
                )
            for key, value in DEFAULT_PROFILE.items():
                connection.execute(
                    "INSERT OR IGNORE INTO profile(key, value, updated_at) VALUES(?, ?, ?)",
                    (key, value, _now()),
                )

    def profile(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM profile").fetchall()
        result = dict(DEFAULT_PROFILE)
        result.update({str(row["key"]): str(row["value"]) for row in rows})
        return result

    def update_profile(self, values: dict[str, object]) -> dict[str, str]:
        allowed = set(DEFAULT_PROFILE)
        with self._connect() as connection:
            for key, raw_value in values.items():
                if key not in allowed:
                    continue
                value = str(raw_value).strip()[:200]
                if not value:
                    continue
                connection.execute(
                    """
                    INSERT INTO profile(key, value, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, value, _now()),
                )
        self._backup_latest()
        return self.profile()

    @staticmethod
    def normalize_memory(content: str) -> str:
        return re.sub(r"[\W_]+", "", content, flags=re.UNICODE).lower()

    def add_memory(
        self,
        content: str,
        *,
        category: str = "explicit",
        importance: int = 5,
        source: str = "explicit",
        confirmed: bool = True,
    ) -> dict[str, object]:
        value = content.strip()[:1000]
        if not value:
            raise ValueError("记忆内容不能为空")
        if category not in MEMORY_CATEGORIES:
            category = "explicit"
        importance = max(1, min(int(importance), 5))
        created_at = _now()
        normalized = self.normalize_memory(value)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, mention_count, confirmed, importance FROM memories
                WHERE normalized=? AND active=1 ORDER BY id DESC LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if existing is not None:
                memory_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE memories
                    SET mention_count=?, last_seen_at=?, updated_at=?,
                        importance=?, confirmed=?, source=?, category=?
                    WHERE id=?
                    """,
                    (
                        int(existing["mention_count"]) + 1,
                        created_at,
                        created_at,
                        max(int(existing["importance"]), importance),
                        int(bool(existing["confirmed"]) or confirmed),
                        source if confirmed else "reinforced",
                        category,
                        memory_id,
                    ),
                )
                is_new = False
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO memories(
                        created_at, content, active, category, importance, source,
                        confirmed, normalized, mention_count, last_seen_at, updated_at
                    ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        created_at,
                        value,
                        category,
                        importance,
                        source,
                        int(confirmed),
                        normalized,
                        created_at,
                        created_at,
                    ),
                )
                memory_id = int(cursor.lastrowid)
                is_new = True
        self._backup_latest()
        memory = self.memory(memory_id)
        memory["is_new"] = is_new
        return memory

    def memory(self, memory_id: int) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, created_at, content, category, importance, source,
                       confirmed, mention_count, last_seen_at, updated_at
                FROM memories WHERE id=?
                """,
                (memory_id,),
            ).fetchone()
        if row is None:
            raise ValueError("记忆不存在")
        result = dict(row)
        result["confirmed"] = bool(result["confirmed"])
        result["category_label"] = MEMORY_CATEGORIES.get(str(result["category"]), "其他")
        return result

    def memories(self, limit: int = 50) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, content, category, importance, source,
                       confirmed, mention_count, last_seen_at, updated_at
                FROM memories WHERE active=1
                ORDER BY confirmed DESC, importance DESC, last_seen_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["confirmed"] = bool(item["confirmed"])
            item["category_label"] = MEMORY_CATEGORIES.get(str(item["category"]), "其他")
            result.append(item)
        return result

    @staticmethod
    def _memory_terms(value: str) -> set[str]:
        terms: set[str] = set()
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", value.lower()):
            if re.fullmatch(r"[a-z0-9]+", token):
                if len(token) >= 2:
                    terms.add(token)
                continue
            if len(token) <= 2:
                terms.add(token)
                continue
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
            terms.update(token[index:index + 3] for index in range(len(token) - 2))
        return terms

    def relevant_memories(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        candidates = [item for item in self.memories(200) if item["confirmed"]]
        if not candidates:
            return []
        maximum = max(1, min(int(limit), 20))
        normalized_query = self.normalize_memory(query)
        query_terms = self._memory_terms(query)
        ranked: list[tuple[float, dict[str, object]]] = []
        for item in candidates:
            content = str(item["content"])
            normalized_content = self.normalize_memory(content)
            content_terms = self._memory_terms(content)
            overlap = query_terms & content_terms
            relevance = float(len(overlap) * 12)
            if overlap and query_terms:
                relevance += 30 * len(overlap) / len(query_terms)
            if normalized_query and normalized_content:
                if normalized_query in normalized_content or normalized_content in normalized_query:
                    relevance += 80
            relevance += int(item["importance"]) * 2
            relevance += min(int(item["mention_count"]), 5)
            ranked.append((relevance, item))
        ranked.sort(
            key=lambda pair: (
                pair[0],
                int(pair[1]["importance"]),
                str(pair[1]["last_seen_at"]),
                int(pair[1]["id"]),
            ),
            reverse=True,
        )

        selected: list[dict[str, object]] = []
        selected_ids: set[int] = set()

        def add(item: dict[str, object]) -> None:
            memory_id = int(item["id"])
            if memory_id not in selected_ids and len(selected) < maximum:
                selected.append(item)
                selected_ids.add(memory_id)

        relevant = [item for score, item in ranked if score >= 45]
        if relevant:
            anchors = [
                item for _, item in ranked
                if str(item["category"]) in {"identity", "relationship"}
            ]
            reserved = min(2, len(anchors), max(0, maximum - 1))
            for item in relevant[:maximum - reserved]:
                add(item)
            for item in anchors[:2]:
                add(item)
            for item in relevant:
                add(item)
        else:
            for _, item in ranked[:min(maximum, 6)]:
                add(item)
        return selected

    def confirm_memory(self, memory_id: int) -> dict[str, object]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories SET confirmed=1, source='confirmed', updated_at=?
                WHERE id=? AND active=1
                """,
                (_now(), memory_id),
            )
        if cursor.rowcount == 0:
            raise ValueError("记忆不存在")
        self._backup_latest()
        return self.memory(memory_id)

    def forget_memory(self, memory_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE memories SET active=0 WHERE id=? AND active=1",
                (memory_id,),
            )
        if cursor.rowcount > 0:
            self._backup_latest()
        return cursor.rowcount > 0

    def add_message(self, role: str, content: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations(created_at, role, content) VALUES(?, ?, ?)",
                (_now(), role, content.strip()[:4000]),
            )

    def conversations(self, limit: int = 30) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, role, content FROM conversations
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]


class MemoryEngine:
    SENSITIVE_MARKERS = (
        "密码", "验证码", "支付密码", "银行卡", "身份证", "护照号",
        "家庭住址", "详细地址", "私钥", "api key", "apikey", "token",
    )
    COMMAND_MARKERS = (
        "开始监控", "开启监控", "启动监控", "停止监控", "关闭监控",
        "监控状态", "今天有几个", "本月利润", "这个月利润", "查看记忆",
    )
    RULES = (
        ("preference", 3, re.compile(r"我(?:更|最)?(?:喜欢|偏好|倾向于).{1,100}|我(?:不喜欢|讨厌).{1,100}")),
        ("habit", 3, re.compile(r"我(?:通常|经常|习惯|总是).{1,100}")),
        ("project", 3, re.compile(r"我(?:正在做|在做|计划做|打算做|以后想做).{1,120}")),
        ("relationship", 4, re.compile(r".{0,40}(?:对我很重要|我不想失去|像我的助手一样).{0,80}")),
        ("identity", 4, re.compile(r"我叫.{1,30}|(?:叫我|称呼我).{1,30}")),
        ("experience", 3, re.compile(r"我(?:曾经|之前|今天|昨天).{2,120}")),
    )

    @classmethod
    def extract(cls, message: str) -> list[dict[str, object]]:
        value = " ".join(message.strip().split())
        lower = value.lower()
        if not value or value.startswith("记住"):
            return []
        if any(marker in lower for marker in cls.SENSITIVE_MARKERS):
            return []
        compact = "".join(value.split())
        if any(marker in compact for marker in cls.COMMAND_MARKERS):
            return []
        if value.endswith(("?", "？", "吗", "吗？", "呢", "呢？")):
            return []
        candidates: list[dict[str, object]] = []
        seen: set[str] = set()
        for category, importance, pattern in cls.RULES:
            for match in pattern.finditer(value):
                content = match.group(0).strip(" ，。！？；,.!?;")
                normalized = RobotRepository.normalize_memory(content)
                if len(content) < 4 or normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append(
                    {
                        "content": content,
                        "category": category,
                        "importance": importance,
                        "source": "automatic",
                        "confirmed": False,
                    }
                )
        return candidates[:3]


class DialogueProvider:
    def __init__(self, settings: dict, root: Path, logger: logging.Logger):
        self.settings = settings
        self.root = root
        self.log = logger
        self.last_error = ""
        self.last_success_at = ""
        self._http_session = requests.Session()
        self._request_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled"))

    @property
    def configured(self) -> bool:
        endpoint = str(self.settings.get("endpoint", "")).strip()
        model = str(self.settings.get("model", "")).strip()
        return self.enabled and bool(endpoint and model and (self._api_key() or self._is_loopback(endpoint)))

    @staticmethod
    def _is_loopback(endpoint: str) -> bool:
        return urlparse(endpoint).hostname in {"127.0.0.1", "localhost", "::1"}

    def _key_path(self) -> Path:
        return self.root / str(
            self.settings.get("encrypted_key_file", "secrets/robot_dialogue_key.dat")
        )

    def _api_key(self) -> str:
        encrypted_path = self._key_path()
        if encrypted_path.is_file():
            try:
                return unprotect_named_secret(
                    encrypted_path.read_text(encoding="ascii").strip(),
                    "robot-dialogue-key",
                )
            except (OSError, SecretProtectionError) as error:
                self.log.warning("机器人云端密钥无法读取：%s", error)
                return ""
        legacy = self.root / str(self.settings.get("api_key_file", "secrets/robot_api_key.txt"))
        return legacy.read_text(encoding="utf-8-sig").strip() if legacy.is_file() else ""

    def _memory_text(self, memories: list[dict[str, object]]) -> str:
        remaining = int(self.settings.get("memory_character_budget", 700))
        lines = []
        for item in memories[:12]:
            content = " ".join(str(item.get("content", "")).split())[:180]
            if not content:
                continue
            if len(content) + 2 > remaining:
                break
            category = MEMORY_CATEGORIES.get(str(item.get("category", "")), "记忆")
            line = f"[{category}] {content}"
            lines.append(f"- {line}")
            remaining -= len(line) + 2
        return "\n".join(lines) or "- 暂无"

    def _history_messages(self, history: list[dict[str, object]]) -> list[dict[str, str]]:
        remaining = int(self.settings.get("history_character_budget", 1600))
        selected = []
        for item in reversed(history[-10:]):
            role = str(item.get("role", ""))
            # Assistant history is model-generated and may contain stale or
            # unverified claims. Keep user intent for continuity, while all
            # factual answers must come from local skills or current data.
            if role != "user":
                continue
            content = " ".join(str(item.get("content", "")).split())[:500]
            if not content:
                continue
            if len(content) > remaining:
                content = content[:remaining]
            if not content:
                break
            selected.append({"role": role, "content": content})
            remaining -= len(content)
            if remaining <= 0 or len(selected) >= 6:
                break
        return list(reversed(selected))

    def _request_parts(
        self,
        message: str,
        profile: dict[str, str],
        memories: list[dict[str, object]],
        history: list[dict[str, object]],
        emotion_style: str = "",
    ) -> tuple[str, dict[str, str], dict[str, object], float] | None:
        if not self.enabled:
            return None
        endpoint = str(self.settings.get("endpoint", "")).strip()
        model = str(self.settings.get("model", "")).strip()
        if not endpoint or not model:
            self.log.warning("机器人对话模型配置不完整，使用本地基础对话")
            return None
        api_key = self._api_key()
        if not api_key and not self._is_loopback(endpoint):
            self.log.warning("远程对话接口没有配置密钥，拒绝调用")
            return None
        memory_text = self._memory_text(memories)
        mission = str(profile.get("mission") or DEFAULT_PROFILE["mission"])
        system = (
            f"你叫{profile['name']}，是{profile['owner_name']}长期信赖的{profile['role']}。"
            f"你的性格是：{profile['personality']}。你不是客服、播音员或解说词生成器，而是一个真实、熟悉的人。"
            f"你存在的意义是：{mission}。不要只给空泛安慰，应尽量给出真正可执行的帮助。"
            "用日常中文口语交流，直接接住对方刚说的话。短问题通常只回一两句，复杂问题再展开。"
            "默认只回答一句并控制在四十个汉字以内；只有对方明确要求详细解释、步骤或清单时才展开。"
            "句子长短要自然，有明确态度时就直说；不确定时可以自然地说“我得想想”或“这个我不敢确定”，但不要滥用口头禅。"
            "禁止使用客服套话和汇报腔，不要用“好的”“当然可以”“首先、其次、最后”“综上所述”“希望对你有帮助”开场或收尾。"
            "除非对方明确要步骤或清单，否则不要编号罗列。不要每句话重复身份，也不要为了显得亲近而夸张。"
            "不要自称 AI、模型或系统，除非对方直接问到。诚实说明不知道或做不到的事。"
            "涉及付款、删除数据、发送消息或控制设备时必须明确确认。"
            "回答适合直接朗读，除非必要不要使用表格和复杂 Markdown。不要朗读文件路径、文件夹清单或大段原文；文件操作只说数量和最重要的结论。"
            "语音转写可能出现同音字，应结合上下文理解常见中国地名，例如湖北的‘先逃、先桃、仙逃’均指仙桃。以下是已确认的长期记忆：\n"
            f"{memory_text}"
        )
        if emotion_style.strip():
            system += f"\n当前情绪基调：{emotion_style.strip()}。"
        messages = [{"role": "system", "content": system}]
        messages.extend(self._history_messages(history))
        messages.append({"role": "user", "content": message[:1000]})
        request_payload: dict[str, object] = {"model": model, "messages": messages}
        provider = str(self.settings.get("provider", "custom"))
        output_limit = max(80, min(int(self.settings.get("max_output_tokens", 220)), 600))
        if provider == "deepseek":
            request_payload["thinking"] = {"type": "disabled"}
            request_payload["max_tokens"] = output_limit
        elif provider == "openai":
            request_payload["max_completion_tokens"] = output_limit
        elif provider == "qwen":
            request_payload["enable_thinking"] = False
            request_payload["max_tokens"] = output_limit
        else:
            request_payload["max_tokens"] = output_limit
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = float(self.settings.get("timeout_seconds", 30))
        return endpoint, headers, request_payload, timeout

    def reply(
        self,
        message: str,
        profile: dict[str, str],
        memories: list[dict[str, object]],
        history: list[dict[str, object]],
        emotion_style: str = "",
    ) -> str | None:
        request = self._request_parts(message, profile, memories, history, emotion_style)
        if request is None:
            return None
        endpoint, headers, request_payload, timeout = request
        try:
            with self._request_lock:
                response = self._http_session.post(
                    endpoint,
                    headers=headers,
                    json=request_payload,
                    timeout=timeout,
                )
            response.raise_for_status()
            result = response.json()
            reply = str(result["choices"][0]["message"]["content"]).strip()
            if not reply:
                raise ValueError("模型返回空内容")
            self.last_error = ""
            self.last_success_at = _now()
            return reply
        except (
            requests.RequestException,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            self.last_error = str(error)[:300]
            self.log.warning("机器人对话模型调用失败，使用本地基础对话：%s", error)
            return None

    def stream_reply(
        self,
        message: str,
        profile: dict[str, str],
        memories: list[dict[str, object]],
        history: list[dict[str, object]],
        emotion_style: str = "",
    ) -> Iterator[str]:
        request = self._request_parts(message, profile, memories, history, emotion_style)
        if request is None:
            return
        endpoint, headers, request_payload, timeout = request
        request_payload["stream"] = True
        received = False
        try:
            with self._request_lock:
                with self._http_session.post(
                    endpoint,
                    headers=headers,
                    json=request_payload,
                    timeout=timeout,
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    for raw_line in response.iter_lines(chunk_size=64):
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line)
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        event = json.loads(data)
                        choices = event.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        choice = choices[0]
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if not content and isinstance(choice.get("message"), dict):
                            content = choice["message"].get("content")
                        if isinstance(content, str) and content:
                            received = True
                            yield content
            if not received:
                raise ValueError("模型返回空内容")
            self.last_error = ""
            self.last_success_at = _now()
        except (
            requests.RequestException,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            self.last_error = str(error)[:300]
            self.log.warning("机器人流式对话调用失败：%s", error)


class RobotCore:
    def __init__(
        self,
        root: Path,
        config: dict,
        logger: logging.Logger,
        config_path: Path | None = None,
    ):
        self.root = root
        self.config = config
        self.config_path = config_path or root / "robot_config.json"
        self.log = logger
        runtime = root / config.get("runtime_dir", "runtime")
        self.robot_repository = RobotRepository(
            runtime / "robot.sqlite3",
            runtime / "robot-backups",
        )
        price_monitor = config.get("price_monitor")
        if price_monitor:
            price_root = Path(str(price_monitor["root"])).resolve()
            price_runtime = price_root / str(
                price_monitor.get("runtime_dir", "runtime")
            )
            self.app_repository = AppRepository(price_runtime / "monitor.sqlite3")
            self.monitor = TaskController(price_root)
        else:
            self.app_repository = AppRepository(runtime / "monitor.sqlite3")
            self.monitor = TaskController(root)
        self.body = RobotBodyController(config.get("body", {}), logger)
        self.companion = CompanionManager(runtime / "robot.sqlite3", logger)
        self.guess_number = GuessNumberGame()
        self.rps = RockPaperScissors()
        self.patrol = PatrolController(
            lambda **kwargs: self.body.command(**kwargs),
            logger,
        )
        gateway_settings = config.get("device_gateway", {})
        self.devices = XiaozhiDeviceRegistry(
            timeout_seconds=float(gateway_settings.get("presence_timeout_seconds", 45)),
            endpoint_silence_ms=int(gateway_settings.get("endpoint_silence_ms", 450)),
            conversation_timeout_seconds=float(
                gateway_settings.get("conversation_timeout_seconds", 10)
            ),
        )
        self.device_gateway = None
        self.computer_access = ComputerAccessManager(
            root,
            runtime,
            config.get("computer_access", {}),
            logger,
        )
        self._last_file_context: Path | None = None
        self._weather_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._weather_lock = threading.Lock()
        self.dialogue = DialogueProvider(config.get("dialogue", {}), root, logger)
        self.memory_engine = MemoryEngine()
        self.speech = SpeechSynthesizer(
            config.get("speech", {}),
            runtime / "robot-audio-cache",
            logger,
            root,
        )
        # Only start the background TTS warm-up when the setting is explicit.
        # This avoids leaving a provider-owned temporary audio file open for
        # callers/tests that construct RobotCore with a minimal config.
        if self.speech.enabled and bool(
            config.get("speech", {}).get("preload_acknowledgement", False)
        ):
            threading.Thread(
                target=self._preload_wake_ack,
                name="robot-wake-ack-preload",
                daemon=True,
            ).start()
        recognition_settings = config.get("recognition", {})
        self.recognizer = SpeechRecognizer(
            recognition_settings,
            root / str(recognition_settings.get("model_dir", "runtime/robot-models/asr")),
            runtime / "robot-audio-upload",
            logger,
        )
        if recognition_settings and bool(recognition_settings.get("preload", True)):
            threading.Thread(
                target=self.recognizer.preload,
                name="robot-speech-preload",
                daemon=True,
            ).start()
        if bool(config.get("weather", {}).get("preload", False)):
            threading.Thread(
                target=self._preload_weather,
                name="robot-weather-preload",
                daemon=True,
            ).start()

    def _preload_wake_ack(self) -> None:
        try:
            for phrase in ("我在", "我想一下。", "在等你啊，你说。"):
                self.speech.synthesize(phrase)
            self.log.info("唤醒和思考回应语音已预热")
        except Exception as error:
            self.log.warning("唤醒回应语音预热失败：%s", error)

    def _preload_weather(self) -> None:
        try:
            reply = self._weather_reply("今天天气怎么样")
            if reply:
                self.log.info("默认城市天气已预取")
        except Exception as error:
            self.log.warning("默认城市天气预取失败：%s", error)

    @staticmethod
    def _alert_payload(alert) -> dict[str, object]:
        payload = asdict(alert)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in payload.items()
        }

    def alerts(self, limit: int = 20) -> list[dict[str, object]]:
        return [
            self._alert_payload(alert)
            for alert in self.app_repository.alerts(
                limit=max(100, min(max(limit, 1) * 5, 500))
            )
            if alert.alert_level in {"A+", "A"}
        ][:limit]

    def status(self) -> dict[str, object]:
        summary = self.app_repository.dashboard_summary()
        return {
            "protocol_version": ROBOT_PROTOCOL_VERSION,
            "price_monitor_skill_version": PRICE_MONITOR_SKILL_VERSION,
            "profile": self.robot_repository.profile(),
            "monitor_running": self.monitor.is_running(),
            "dialogue_online": self.dialogue.configured and not self.dialogue.last_error,
            "dialogue_configured": self.dialogue.configured,
            "speech_online": self.speech.enabled,
            "recognition_online": self.recognizer.enabled,
            "device": self.devices.status(),
            "device_gateway": self.device_gateway.status() if self.device_gateway else {"running": False},
            "computer_access": self.computer_access.status(),
            "wake": {
                "word": str(self.recognizer.settings.get("wake_word", "小帮")),
                "default_sensitivity": max(
                    1,
                    min(5, int(self.recognizer.settings.get("wake_default_sensitivity", 3))),
                ),
            },
            "voiceprint": self.recognizer.voiceprint_status(),
            "voiceprint_last_sample": self.recognizer.last_physical_voiceprint_sample(),
            "body": self.body.status(),
            "companion": self.companion.summary(),
            "patrol": self.patrol.status(),
            "summary": {key: str(value) if isinstance(value, Decimal) else value for key, value in summary.items()},
            "server_time": _now(),
        }

    def control_body(self, payload: dict) -> dict[str, object]:
        motion = payload.get("motion")
        expression = payload.get("expression")
        pan_deg = payload.get("pan_deg")
        tilt_deg = payload.get("tilt_deg")
        micro_action = payload.get("micro_action")
        return self.body.command(
            motion=str(motion) if motion is not None else None,
            expression=str(expression) if expression is not None else None,
            pan_deg=float(pan_deg) if pan_deg is not None else None,
            tilt_deg=float(tilt_deg) if tilt_deg is not None else None,
            speed=int(payload.get("speed", 45)),
            duration_ms=int(payload.get("duration_ms", 800)),
            micro_action=str(micro_action) if micro_action is not None else None,
            emotion_intensity=float(payload.get("emotion_intensity", 1.0)),
            follow_up=bool(payload.get("follow_up", False)),
            exit_transition=bool(payload.get("exit_transition", False)),
        )

    def dialogue_settings(self) -> dict[str, object]:
        settings = self.dialogue.settings
        provider = str(settings.get("provider", "custom"))
        if provider not in DIALOGUE_PROVIDERS:
            provider = "custom"
        key_path = self.dialogue._key_path()
        legacy_path = self.root / str(settings.get("api_key_file", "secrets/robot_api_key.txt"))
        return {
            "enabled": self.dialogue.enabled,
            "configured": self.dialogue.configured,
            "provider": provider,
            "endpoint": str(settings.get("endpoint", "")),
            "model": str(settings.get("model", "")),
            "has_key": key_path.is_file() or legacy_path.is_file(),
            "last_error": self.dialogue.last_error,
            "last_success_at": self.dialogue.last_success_at,
            "providers": [
                {"id": provider_id, **definition}
                for provider_id, definition in DIALOGUE_PROVIDERS.items()
            ],
        }

    def update_dialogue_settings(self, values: dict[str, object]) -> dict[str, object]:
        provider_id = str(values.get("provider", "")).strip()
        definition = DIALOGUE_PROVIDERS.get(provider_id)
        if definition is None:
            raise ValueError("不支持这个云端模型供应商")
        current_provider = str(self.dialogue.settings.get("provider", ""))
        supplied_key = str(values.get("api_key", "")).strip()
        if provider_id != current_provider and not supplied_key:
            raise ValueError("切换供应商时需要填写新的 API 密钥")
        if definition.get("allow_custom_endpoint"):
            endpoint = str(values.get("endpoint", "") or definition["endpoint"]).strip()
        else:
            endpoint = str(definition["endpoint"] or values.get("endpoint", "")).strip()
        model = str(values.get("model", "") or definition["default_model"]).strip()
        if not model or len(model) > 100:
            raise ValueError("模型名称无效")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("云端接口必须使用有效的 HTTPS 地址")
        if len(endpoint) > 500:
            raise ValueError("云端接口地址过长")
        encrypted_key_file = "secrets/robot_dialogue_key.dat"
        key_path = self.root / encrypted_key_file
        if supplied_key:
            if len(supplied_key) > 1000:
                raise ValueError("API 密钥过长")
            key_path.parent.mkdir(parents=True, exist_ok=True)
            protected = protect_named_secret(supplied_key, "robot-dialogue-key")
            temporary_key = key_path.with_suffix(".tmp")
            temporary_key.write_text(protected, encoding="ascii")
            temporary_key.replace(key_path)
        elif not key_path.is_file():
            raise ValueError("请填写 API 密钥")
        dialogue_settings = {
            "enabled": bool(values.get("enabled", True)),
            "provider": provider_id,
            "endpoint": endpoint,
            "model": model,
            "encrypted_key_file": encrypted_key_file,
            "timeout_seconds": max(10, min(int(values.get("timeout_seconds", 60)), 180)),
            "max_output_tokens": 220,
            "history_character_budget": 1600,
            "memory_character_budget": 700,
        }
        self.config["dialogue"] = dialogue_settings
        temporary_config = self.config_path.with_suffix(".tmp.json")
        temporary_config.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_config.replace(self.config_path)
        self.dialogue.settings = dialogue_settings
        self.dialogue.last_error = ""
        return self.dialogue_settings()

    def test_dialogue(self) -> dict[str, object]:
        if not self.dialogue.configured:
            raise ValueError("云端大脑尚未配置完整")
        reply = self.dialogue.reply(
            "请只用一句自然的中文向我打招呼。",
            self.robot_repository.profile(),
            [item for item in self.robot_repository.memories(30) if item["confirmed"]],
            [],
        )
        if reply is None:
            detail = self.dialogue.last_error or "接口没有返回内容"
            raise ValueError(f"云端大脑连接失败：{detail}")
        return {"reply": reply, "settings": self.dialogue_settings()}

    def _file_path_from_message(self, message: str) -> Path | None:
        status = self.computer_access.status()
        candidates = [Path(value) for value in status.get("allowed_roots", [])]
        for candidate in candidates:
            if str(candidate).casefold() in message.casefold():
                return candidate
        for match in re.finditer(r"(?i)(?:[a-z]:\\|\\\\)", message):
            remainder = message[match.start():].splitlines()[0]
            for end in range(len(remainder), 2, -1):
                value = remainder[:end].strip(" \t\"'，,。！？!?；;、:：")
                if not value:
                    continue
                path = Path(value)
                if path.exists():
                    return path
        return None

    def _file_reply(self, message: str) -> str | None:
        compact = "".join(message.split()).lower()
        intent_words = (
            "文件", "目录", "文件夹", "表格", "excel", "xlsx", "csv",
            "利润加起来", "利润合计", "整理文件", "读取文件", "分析文件",
            "分析目录", "分析这个目录", "分析这个文件夹",
        )
        has_explicit_path = bool(re.search(r"(?i)(?:[a-z]:\\|\\\\)", message))
        if not any(word in compact for word in intent_words) and not has_explicit_path:
            return None
        status = self.computer_access.status()
        if not status["can_read"]:
            return "我可以帮你搜索、读取和分析文件，但目前还没有电脑读取权限。请先在“电脑权限”里授权文件夹并选择“只读”。"

        path = self._file_path_from_message(message)
        if path is None and self._last_file_context is not None and any(
            word in compact for word in ("里面", "这个目录", "这些文件", "刚才", "继续")
        ):
            path = self._last_file_context
        if path is None:
            roots = status.get("allowed_roots", [])
            if len(roots) == 1 and any(word in compact for word in ("授权文件", "授权目录", "这个文件夹")):
                path = Path(str(roots[0]))
        if path is None:
            return "你把已授权的文件夹路径发给我，或者先说‘查看授权目录’，我就能继续处理。"
        self._last_file_context = path

        try:
            if any(word in compact for word in ("利润", "收入", "流水", "合计", "总计", "统计", "分析")):
                result = self.computer_access.analyze_path(str(path))
                totals = result["totals"]
                labels = (
                    ("profit", "利润合计"),
                    ("expected_profit", "预计利润"),
                    ("realized_profit", "已实现利润"),
                    ("derived_profit", "按收入减成本运费推算利润"),
                    ("revenue", "流水"),
                    ("cost", "成本"),
                    ("shipping", "运费"),
                )
                summary = [
                    f"我分析了 {result['files']} 个表格、{result['rows']} 行有效数据。"
                ]
                for key, label in labels:
                    if key in totals:
                        summary.append(f"{label} ¥{totals[key]}")
                if result["specifications"]:
                    summary.append("识别到的规格：" + "、".join(result["specifications"][:20]))
                if len(summary) == 1:
                    summary.append("表格里没有识别到利润、收入、成本或运费列。")
                return "\n".join(summary)

            if any(word in compact for word in ("搜索", "查找", "找一下", "找找")):
                query = re.sub(r"(?i)(搜索|查找|找一下|找找)", "", message, count=1).strip()
                query = query.replace(str(path), "").strip(" \t\"'，,。！？!?；;、")
                query = re.sub(r"(文件|文件夹|目录)$", "", query).strip()
                if not query:
                    return "你想搜索的文件名或关键词是什么？"
                target = path if path.is_dir() else path.parent
                result = self.computer_access.search(query, str(target), limit=12)
                if not result["results"]:
                    return f"我在授权范围内没有找到包含“{query}”的文件。"
                lines = [f"找到了 {len(result['results'])} 项："]
                lines.extend(f"{item['name']}：{item['path']}" for item in result["results"])
                return "\n".join(lines)

            if path.is_dir():
                result = self.computer_access.list_directory(str(path))
                if not result["entries"]:
                    return "这个目录是空的。"
                lines = [f"这个目录有 {len(result['entries'])} 项，先列出前 12 项："]
                lines.extend(f"{item['name']}（{item['kind']}）" for item in result["entries"][:12])
                return "\n".join(lines)
            result = self.computer_access.inspect_file(str(path))
            preview = result.get("preview", {})
            content = str(preview.get("content", "")).strip()
            return f"文件：{result['name']}\n\n{content[:1800] or '这个文件暂时没有可直接预览的文字内容。'}"
        except (PermissionError, FileNotFoundError, ValueError, OSError) as error:
            return f"我暂时不能处理这个文件：{error}"

    def control_monitor(self, action: str) -> str:
        if action == "start":
            if self.monitor.is_running():
                return "价差监控已经在运行。"
            self.monitor.start()
            return "价差监控已经启动。"
        if action == "stop":
            if not self.monitor.is_running():
                return "价差监控当前没有运行。"
            self.monitor.stop()
            return "价差监控已经停止。"
        raise ValueError("不支持的监控操作")

    @staticmethod
    def _weather_description(code: int) -> str:
        if code == 0:
            return "晴"
        if code in {1, 2}:
            return "少云" if code == 1 else "多云"
        if code == 3:
            return "阴"
        if code in {45, 48}:
            return "有雾"
        if code in {51, 53, 55, 56, 57}:
            return "有毛毛雨"
        if code in {61, 63, 65, 66, 67, 80, 81, 82}:
            return "有雨"
        if code in {71, 73, 75, 77, 85, 86}:
            return "有雪"
        if code in {95, 96, 99}:
            return "有雷雨"
        return "天气情况不稳定"

    def _weather_reply(self, message: str) -> str | None:
        compact = "".join(message.split())
        if not any(word in compact for word in ("天气", "温度", "下雨", "降雨", "冷不冷", "热不热")):
            return None
        settings = self.config.get("weather", {})
        known = {
            "仙桃": (30.3625, 113.4545),
            "武汉": (30.5928, 114.3055),
            "北京": (39.9042, 116.4074),
            "上海": (31.2304, 121.4737),
            "广州": (23.1291, 113.2644),
            "深圳": (22.5431, 114.0579),
            "杭州": (30.2741, 120.1551),
        }
        city = next((name for name in known if name in compact), "")
        if not city:
            city = str(settings.get("default_city", "仙桃")).strip() or "仙桃"
        coordinates = known.get(city)
        if coordinates is None:
            try:
                geocoding = requests.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1, "language": "zh", "format": "json"},
                    timeout=float(settings.get("timeout_seconds", 3)),
                )
                geocoding.raise_for_status()
                result = (geocoding.json().get("results") or [])[0]
                coordinates = (float(result["latitude"]), float(result["longitude"]))
                city = str(result.get("name") or city)
            except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
                return f"我暂时没找到{city}的位置，你换成城市名再问我。"
        cache_key = f"{city}:{'tomorrow' if '明天' in compact else 'today'}"
        now = datetime.now().timestamp()
        with self._weather_lock:
            cached = self._weather_cache.get(cache_key)
        if cached and now - cached[0] < float(settings.get("cache_seconds", 600)):
            data = cached[1]
        else:
            try:
                response = requests.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": coordinates[0],
                        "longitude": coordinates[1],
                        "current": "temperature_2m,apparent_temperature,weather_code",
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                        "timezone": "auto",
                        "forecast_days": 2,
                    },
                    timeout=float(settings.get("timeout_seconds", 3)),
                )
                response.raise_for_status()
                data = response.json()
                with self._weather_lock:
                    self._weather_cache[cache_key] = (now, data)
            except (requests.RequestException, TypeError, ValueError):
                return "天气服务暂时连不上，过一会儿再问我。"
        try:
            day_index = 1 if "明天" in compact else 0
            daily = data["daily"]
            description = self._weather_description(int(daily["weather_code"][day_index]))
            low = round(float(daily["temperature_2m_min"][day_index]))
            high = round(float(daily["temperature_2m_max"][day_index]))
            rain = round(float(daily["precipitation_probability_max"][day_index]))
            if day_index == 1:
                return f"{city}明天{description}，{low}到{high}度，最高降雨概率{rain}%。"
            current = data["current"]
            temperature = round(float(current["temperature_2m"]))
            apparent = round(float(current["apparent_temperature"]))
            return f"{city}现在{temperature}度，体感{apparent}度，{description}；今天{low}到{high}度，降雨概率{rain}%。"
        except (KeyError, IndexError, TypeError, ValueError):
            return "天气数据刚才不完整，过一会儿再问我。"

    @staticmethod
    def is_conversation_exit(message: str) -> bool:
        compact = re.sub(r"[\s，。！？!?、：:；;,.]+", "", str(message or "")).lower()
        exact_phrases = {
            "晚安",
            "没事了",
            "我没事了",
            "可以了",
            "行了",
            "就这样",
            "先这样",
            "到这吧",
            "你退下吧",
            "退下吧",
        }
        ending_phrases = (
            "结束对话",
            "退出对话",
            "暂停对话",
            "不聊了",
            "先不聊了",
            "不说了",
            "先不说了",
            "不用听了",
            "先别听了",
            "你先退下",
            "我要睡觉了",
            "我睡觉了",
            "我要睡了",
            "我先睡了",
            "准备睡觉了",
            "先休息了",
            "我要休息了",
            "今天先这样",
            "今天到这",
        )
        if compact in exact_phrases or any(phrase in compact for phrase in ending_phrases):
            return True
        # Streaming ASR may prepend the wake word or append a modal particle.
        # These explicit stop words are safe to honor while conversation mode
        # is active and prevent the listener from immediately reopening.
        stop_words = any(
            word in compact
            for word in (
                "退下",
                "睡觉",
                "睡了",
                "晚安",
                "就这样",
                "先这样",
                "不用听",
                "别听",
                "停止监听",
                "停止聆听",
                "结束对话",
                "退出对话",
                "不聊",
                "不说了",
                "到此为止",
                "到这里",
                "闭麦",
                "安静",
            )
        )
        return stop_words and not any(phrase in compact for phrase in ("设置睡觉", "睡觉提醒", "提醒我睡"))

    def _local_reply(self, message: str) -> str | None:
        compact = "".join(message.split())
        if self.is_conversation_exit(compact):
            if any(word in compact for word in ("睡觉", "睡了", "晚安", "休息")):
                return "好，晚安，需要我时再叫我。"
            return "好，需要我时再叫我。"
        if any(
            phrase in compact
            for phrase in ("现在几点", "现在时间", "当前时间", "北京时间", "几点了", "报时")
        ):
            now = _beijing_now()
            period = (
                "凌晨" if now.hour < 6 else
                "上午" if now.hour < 12 else
                "下午" if now.hour < 18 else
                "晚上"
            )
            return f"现在是北京时间{period}{now.hour % 12 or 12}点{now.minute:02d}分。"
        if compact in {"你在干嘛", "你在干什么", "你干嘛呢", "你在做什么"}:
            return "在等你啊，你说。"
        weather_reply = self._weather_reply(message)
        if weather_reply is not None:
            return weather_reply
        file_reply = self._file_reply(message)
        if file_reply is not None:
            return file_reply
        movement_reply = self._local_movement_reply(compact)
        if movement_reply is not None:
            return movement_reply
        guess_match = re.fullmatch(r"(?:我猜|猜)(?:是)?(\d+)", compact)
        if compact in {"开始猜数字", "猜数字"}:
            try:
                return self.guess_number.start()["reply"]
            except ValueError as error:
                return str(error)
        if compact in {"停止猜数字", "结束猜数字"}:
            self.guess_number.active = False
            return "好，这次猜数字先到这里。"
        if guess_match is not None:
            try:
                return self.guess_number.guess(int(guess_match.group(1)))["reply"]
            except ValueError as error:
                return str(error)
        if compact in {"石头", "锤子", "布", "剪刀", "剪子"}:
            try:
                result = self.rps.play(compact)
            except ValueError as error:
                return str(error)
            if result["result"] in {"win", "lose"}:
                try:
                    self.body.wiggle()
                except Exception:
                    pass
            return result["reply"]
        if compact in {"开始巡逻", "开启巡逻"}:
            self.patrol.start()
            return "我开始在桌面慢慢巡逻，碰到东西会绕开。"
        if compact in {"停止巡逻", "关闭巡逻", "结束巡逻"}:
            self.patrol.stop()
            return "好，我停下，不再巡逻了。"
        if compact in {"碰到障碍", "前方有障碍", "有障碍"}:
            return self.patrol.obstacle()
        if compact in {"桌面震动", "桌子震动", "地震了"}:
            return self.patrol.vibration()
        if any(
            phrase in compact
            for phrase in ("刚才聊过什么", "昨天聊过什么", "之前聊过什么", "我们聊过什么")
        ):
            topic = self.companion.recall_topic()
            if topic:
                return str(topic["suggestion"])
            return "我这边还没有存下可以提起的聊天片段。"
        if compact in {"开始监控", "开启监控", "启动监控"}:
            return self.control_monitor("start")
        if compact in {"停止监控", "关闭监控", "结束监控"}:
            return self.control_monitor("stop")
        if "监控" in compact and any(word in compact for word in ("状态", "运行", "开着")):
            return "价差监控正在运行。" if self.monitor.is_running() else "价差监控当前没有运行。"
        if compact.startswith("记住"):
            content = message.replace("记住：", "", 1).replace("记住:", "", 1)
            if content.strip() == message.strip():
                content = message.strip()[2:]
            memory = self.robot_repository.add_memory(content)
            return f"我记住了：{memory['content']}"
        if any(phrase in compact for phrase in ("你记得什么", "查看记忆", "我的记忆")):
            memories = [
                memory for memory in self.robot_repository.memories(30)
                if memory["confirmed"]
            ][:8]
            if not memories:
                return "目前还没有保存长期记忆。"
            return "我现在记得：\n" + "\n".join(f"{index}. {item['content']}" for index, item in enumerate(memories, 1))
        if any(phrase in compact for phrase in ("仙桃物流优化项目", "仙桃项目")) and any(
            word in compact for word in ("查", "看", "进展", "方案", "状态")
        ):
            return "我这边没有查到“仙桃物流优化项目”的真实记录。"
        has_alert_topic = any(word in compact for word in ("A级", "A+", "报警", "方案"))
        has_alert_query = any(
            word in compact
            for word in ("今天", "最近", "最新", "当前", "现在", "看一下", "查一下", "有几个")
        )
        if has_alert_topic and has_alert_query:
            alerts = self.alerts(100)
            is_today = "今天" in compact
            if is_today:
                today = datetime.now().astimezone().date().isoformat()
                alerts = [alert for alert in alerts if str(alert["created_at"]).startswith(today)]
            if not alerts:
                return "今天还没有发现 A+ / A 优先方案。" if is_today else "最近没有查到 A+ / A 优先方案。"
            period = "今天" if is_today else "最近"
            lines = [f"{period}发现 {len(alerts)} 个 A+ / A 优先方案。"]
            lines.extend(
                f"{item['sku']}，规格{item['size']}，预计利润 ¥{item['profit']}"
                for item in alerts[:5]
            )
            return "\n".join(lines)
        if any(word in compact for word in ("本月利润", "这个月利润", "本月流水", "这个月流水")):
            month = datetime.now().astimezone().strftime("%Y-%m")
            summary = self.app_repository.month_summary(month)
            return (
                f"本月共 {summary['count']} 笔订单，预计利润 ¥{summary['expected_profit']}，"
                f"已实现利润 ¥{summary['realized_profit']}，已售流水 ¥{summary['sold_revenue']}。"
            )
        return None

    def _local_movement_reply(self, compact: str) -> str | None:
        question_words = ("怎么", "如何", "为什么", "能不能", "会不会", "可以吗", "吗？", "吗?")
        if any(word in compact for word in question_words):
            return None
        stop_phrases = ("停下", "别动", "不要动", "停止移动", "停止走动", "站住")
        if any(phrase in compact for phrase in stop_phrases):
            self.body.command(motion="stop")
            return "好，我停下了。"
        if any(negation in compact for negation in ("不要前进", "别前进", "不要后退", "别后退", "不要转", "别转")):
            self.body.command(motion="stop")
            return "好，我不动。"
        if any(phrase in compact for phrase in ("头回正", "脑袋回正", "头部回正", "恢复正面")):
            self.body.command(pan_deg=90, tilt_deg=90)
            return "好，我把头回正。"
        if any(phrase in compact for phrase in ("向左转头", "头向左", "脑袋向左", "左边看看")):
            self.body.command(pan_deg=120)
            return "好，我向左看看。"
        if any(phrase in compact for phrase in ("向右转头", "头向右", "脑袋向右", "右边看看")):
            self.body.command(pan_deg=60)
            return "好，我向右看看。"
        if any(phrase in compact for phrase in ("抬头", "头抬起来", "向上看", "低头", "头低下来", "向下看")):
            return "我只有左右转头的舵机，不能抬头低头。"
        motions = (
            ("forward", "向前", ("向前移动", "往前移动", "向前走", "往前走", "向前开", "往前开", "前进")),
            ("backward", "后退", ("向后移动", "往后移动", "向后走", "往后走", "向后退", "往后退", "后退")),
            ("left", "向左转", ("向左转", "往左转", "左转", "向左移动")),
            ("right", "向右转", ("向右转", "往右转", "右转", "向右移动")),
        )
        for motion, label, phrases in motions:
            if not any(phrase in compact for phrase in phrases):
                continue
            duration_ms = 700
            if "一点" in compact or "一点点" in compact:
                duration_ms = 500
            elif "两秒" in compact or "2秒" in compact:
                duration_ms = 2000
            elif "一秒" in compact or "1秒" in compact:
                duration_ms = 1000
            self.body.command(motion=motion, speed=40, duration_ms=duration_ms)
            return f"好，我{label}{'一点' if duration_ms == 500 else ''}。"
        return None

    @staticmethod
    def _emotion_for(message: str, reply: str) -> str:
        message_compact = "".join(message.split())
        reply_compact = "".join(reply.split())
        combined = f"{message_compact} {reply_compact}"
        if any(word in combined for word in ("报警", "危险", "紧急", "赶紧")):
            return "alert"
        if any(word in combined for word in ("生气", "愤怒", "恼火", "气死", "烦死")):
            return "angry"
        if any(word in combined for word in ("难受", "焦虑", "担心", "害怕", "亏了", "失败", "不开心", "累坏", "抱歉", "可惜", "暂时无法", "没法")):
            return "concerned"
        if any(word in combined for word in ("太好了", "成功了", "赚了", "开心", "厉害", "完成了")):
            return "excited"
        if any(word in combined for word in ("困了", "睡觉", "晚安", "想休息")):
            return "sleepy"
        if any(word in combined for word in ("谢谢", "解决了", "搞定了", "放心了")):
            return "relieved"
        if any(word in combined for word in ("为什么", "怎么回事", "你觉得", "真的吗", "是什么")) or "？" in message or "?" in message:
            return "curious"
        if any(word in reply for word in ("记住了", "已经启动", "已经停止", "发现")):
            return "happy"
        return "idle"

    def realtime_voice_instructions(self) -> str:
        profile = self.robot_repository.profile()
        memories = [
            item for item in self.robot_repository.memories(30)
            if item.get("confirmed")
        ][:8]
        memory_lines = "\n".join(
            f"- {str(item.get('content', '')).strip()[:160]}"
            for item in memories
            if str(item.get("content", "")).strip()
        ) or "- 暂无稳定记忆"
        return (
            f"你是{profile['name']}，主人是{profile['owner_name']}。"
            f"你的定位是{profile['role']}。说话风格：{profile['personality']}。\n"
            "用自然、简短的中文对话。默认只说一句，最多四十个汉字；只有主人明确要求详细说明时才最多说三句。"
            "第一句话直接给答案，不要复述问题，不要自称模型。不要逐个朗读文件名、文件夹名、路径、清单或大段原文，"
            "文件操作只说数量和最重要的一个结论。语音同音字要结合上下文纠正，例如湖北的先逃、先桃、仙逃都按仙桃理解。"
            "听不清时直接请主人再说一遍。涉及移动、监控、文件、方案或记忆的命令会由本地核心执行，"
            "你只负责自然表达最终结果。不得根据记忆、旧对话或猜测编造方案、项目、数量、监控状态或进度；"
            "没有本地数据时必须直说没有查到。\n"
            f"已确认的长期记忆：\n{memory_lines}\n"
            f"\n当前情绪基调：{self.companion.emotion_style()}。可以自然提起之前聊过的日常话题，"
            "但只引用本地保存的聊天片段，不要凭旧对话编造未确认的内容。"
        )

    @staticmethod
    def spoken_reply(message: str, reply: str) -> str:
        """Keep physical speech brief while preserving the full reply for the UI."""
        value = str(reply or "").strip()
        if not value:
            return value
        compact_message = "".join(str(message or "").split())
        detail_requested = any(
            word in compact_message
            for word in ("详细", "全部", "所有", "逐个", "逐条", "读出来", "念出来", "完整")
        )
        lines = [" ".join(line.split()) for line in value.splitlines() if line.strip()]
        first = lines[0] if lines else value
        if not detail_requested:
            directory = re.match(r"这个目录有\s*(\d+)\s*项", first)
            if directory:
                return f"这个目录有{directory.group(1)}项。你想找哪个文件？"
            found = re.match(r"找到了\s*(\d+)\s*项", first)
            if found:
                return f"找到了{found.group(1)}项。你告诉我想打开哪一个。"
            if first.startswith("我现在记得："):
                return f"我记得{max(0, len(lines) - 1)}件事。你想问哪一件？"
            if first.startswith("今天发现") and len(lines) > 1:
                return first
            if first.startswith("文件："):
                filename = first.removeprefix("文件：").strip()
                return f"我已经打开{filename}。需要我概括内容吗？"
            sentences = re.split(r"(?<=[。！？!?])", value.replace("\n", " "))
            concise = next((part.strip() for part in sentences if part.strip()), first)
            if len(concise) > 60:
                concise = concise[:58].rstrip("，,；;：:") + "。"
            return concise
        return value[:400]

    def realtime_local_chat(self, message: str) -> dict[str, object] | None:
        value = message.strip()[:2000]
        if not value:
            raise ValueError("消息不能为空")
        reply = self._local_reply(value)
        if reply is None:
            return None
        self.robot_repository.add_message("user", value)
        return self._finish_chat(value, reply)

    def finish_realtime_chat(self, message: str, reply: str) -> dict[str, object]:
        value = message.strip()[:2000]
        answer = reply.strip()[:4000]
        if not value or not answer:
            raise ValueError("实时语音对话内容不能为空")
        self.robot_repository.add_message("user", value)
        return self._finish_chat(value, answer)

    def chat(self, message: str) -> dict[str, object]:
        value = self.recognizer._normalize_transcript(message.strip())[:2000]
        if not value:
            raise ValueError("消息不能为空")
        history = self.robot_repository.conversations(20)
        self.robot_repository.add_message("user", value)
        reply = self._local_reply(value)
        if reply is None:
            confirmed_memories = self.robot_repository.relevant_memories(value, 8)
            reply = self.dialogue.reply(
                value,
                self.robot_repository.profile(),
                confirmed_memories,
                history,
                emotion_style=self.companion.emotion_style(),
            )
        if reply is None:
            reply = self._dialogue_fallback()
        return self._finish_chat(value, reply)

    def _dialogue_fallback(self) -> str:
        name = self.robot_repository.profile()["name"]
        if self.dialogue.configured:
            return f"{name}的云端日常对话暂时连不上，基础命令和长期记忆仍然可以使用，请稍后再试。"
        return (
            f"我是{name}。基础命令和长期记忆已经可以使用；开放式日常对话需要在"
            " robot_config.json 中配置对话模型。"
        )

    def _finish_chat(self, message: str, reply: str) -> dict[str, object]:
        memory_updates = []
        for candidate in self.memory_engine.extract(message):
            memory_updates.append(self.robot_repository.add_memory(**candidate))
        self.robot_repository.add_message("assistant", reply)
        compact = "".join(message.split())
        playful = any(
            word in compact
            for word in ("逗你", "你真有意思", "好可爱", "哈哈", "嘿嘿", "来玩", "转个圈", "跳个舞", "笨", "傻")
        )
        fragment = self.companion.observe_message(message)
        if fragment is None:
            self.companion.touch(playful=playful)
        emotion = self._emotion_for(message, reply)
        try:
            self.companion.record_emotion(emotion)
        except Exception as error:
            self.log.warning("情绪氛围记录失败：%s", error)
        return {
            "reply": reply,
            "spoken_reply": self.spoken_reply(message, reply),
            "emotion": emotion,
            "created_at": _now(),
            "memory_updates": memory_updates,
        }

    def stream_chat(
        self,
        message: str,
        on_delta: Callable[[str], None],
        *,
        speech_output: bool = False,
    ) -> dict[str, object]:
        value = message.strip()[:2000]
        if not value:
            raise ValueError("消息不能为空")
        history = self.robot_repository.conversations(20)
        self.robot_repository.add_message("user", value)
        reply = self._local_reply(value)
        if reply is not None:
            on_delta(self.spoken_reply(value, reply) if speech_output else reply)
            return self._finish_chat(value, reply)

        confirmed_memories = self.robot_repository.relevant_memories(value, 8)
        parts = []
        for delta in self.dialogue.stream_reply(
            value,
            self.robot_repository.profile(),
            confirmed_memories,
            history,
            emotion_style=self.companion.emotion_style(),
        ):
            parts.append(delta)
            on_delta(delta)
        reply = "".join(parts).strip()
        if not reply:
            reply = self._dialogue_fallback()
            on_delta(reply)
        return self._finish_chat(value, reply)


class RobotRequestHandler(BaseHTTPRequestHandler):
    server: "RobotHTTPServer"

    def log_message(self, format: str, *args: object) -> None:
        self.server.core.log.debug(format, *args)

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _audio(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _wav_audio(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _stream_audio(self, text: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.server.core.speech.output_content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in self.server.core.speech.stream_text(text):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.server.core.log.debug("浏览器已停止当前语音播放")
        except Exception as error:
            self.server.core.log.warning("流式语音播放中断：%s", error)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self, max_bytes: int = 65536) -> dict:
        if self.headers.get("X-Robot-Client") != "web":
            raise PermissionError("请求来源未授权")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if size <= 0 or size > max_bytes:
            raise ValueError("请求内容为空或过大")
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求格式必须是对象")
        return payload

    def _read_audio(self) -> tuple[bytes, str]:
        if self.headers.get("X-Robot-Client") != "web":
            raise PermissionError("请求来源未授权")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        limit = int(self.server.core.recognizer.settings.get("max_upload_bytes", 10 * 1024 * 1024))
        if size <= 0 or size > limit:
            raise ValueError("录音为空或超过大小限制")
        return self.rfile.read(size), self.headers.get("Content-Type", "")

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        target = (self.server.web_root / relative).resolve()
        if self.server.web_root.resolve() not in target.parents and target != self.server.web_root.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
        }.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/status":
                self._json(self.server.core.status())
                return
            if parsed.path == "/api/alerts":
                limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
                self._json({"alerts": self.server.core.alerts(limit)})
                return
            if parsed.path == "/api/memories":
                self._json({"memories": self.server.core.robot_repository.memories()})
                return
            if parsed.path == "/api/conversations":
                self._json({"messages": self.server.core.robot_repository.conversations(50)})
                return
            if parsed.path == "/api/dialogue/settings":
                self._json(self.server.core.dialogue_settings())
                return
            if parsed.path == "/api/voiceprint/status":
                self._json(self.server.core.recognizer.voiceprint_status())
                return
            if parsed.path == "/api/voiceprint/last-sample":
                self._json(self.server.core.recognizer.last_physical_voiceprint_sample())
                return
            if parsed.path == "/api/body":
                self._json({"body": self.server.core.body.status()})
                return
            if parsed.path == "/api/companion":
                self._json({"companion": self.server.core.companion.summary()})
                return
            if parsed.path == "/api/computer-access":
                self._json(self.server.core.computer_access.status())
                return
            if parsed.path == "/api/computer-access/audit":
                limit = int(parse_qs(parsed.query).get("limit", ["50"])[0])
                self._json({"entries": self.server.core.computer_access.recent_audit(limit)})
                return
            if parsed.path == "/api/files/list":
                path = parse_qs(parsed.query).get("path", [""])[0]
                self._json(self.server.core.computer_access.list_directory(path))
                return
            if parsed.path == "/api/files/search":
                query = parse_qs(parsed.query).get("query", [""])[0]
                path = parse_qs(parsed.query).get("path", [""])[0]
                self._json(self.server.core.computer_access.search(query, path))
                return
            stream_prefix = "/api/audio-stream/"
            if parsed.path.startswith(stream_prefix):
                token = parsed.path[len(stream_prefix):]
                if not re.fullmatch(r"[a-f0-9]{32}", token):
                    raise ValueError("流式语音地址无效")
                text = self.server.core.speech.take_stream(token)
                self._stream_audio(text)
                return
            audio_prefix = "/api/audio/"
            if parsed.path.startswith(audio_prefix):
                filename = parsed.path[len(audio_prefix):]
                if not re.fullmatch(r"[a-f0-9]{64}(?:\.fallback)?\.mp3", filename):
                    raise ValueError("音频地址无效")
                target = self.server.core.speech.cache_dir / filename
                if not target.is_file():
                    self._error(HTTPStatus.NOT_FOUND, "音频不存在")
                    return
                self._audio(target)
                return
            if parsed.path == "/api/wake-ack":
                realtime_settings = (
                    self.server.core.config.get("device_gateway", {})
                    .get("realtime_voice", {})
                )
                files = realtime_settings.get("wake_ack_files", [])
                if not isinstance(files, list) or not files:
                    files = [realtime_settings.get("wake_ack_file", "runtime/wake-ack-tina.wav")]
                try:
                    variant = int(parse_qs(parsed.query).get("variant", ["0"])[0])
                except (TypeError, ValueError):
                    variant = 0
                relative = str(files[variant % len(files)])
                target = self.server.core.root / relative
                if not target.is_file():
                    self._error(HTTPStatus.NOT_FOUND, "唤醒回应音频不存在")
                    return
                self._wav_audio(target)
                return
            self._static(parsed.path)
        except PermissionError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))
        except (OSError, sqlite3.Error, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/transcribe":
                audio, content_type = self._read_audio()
                text = self.server.core.recognizer.transcribe(audio, content_type)
                game = getattr(self.server.core, "guess_number", None)
                allow_short_ack = bool(
                    game is not None
                    and getattr(game, "active", False)
                )
                self._json({
                    "text": text,
                    "ignored": is_noise_transcript(
                        text,
                        allow_short_ack=allow_short_ack,
                    ),
                })
                return
            if parsed.path == "/api/wake":
                audio, content_type = self._read_audio()
                self._json(self.server.core.recognizer.detect_wake(audio, content_type))
                return
            if parsed.path == "/api/voiceprint/enroll":
                audio, content_type = self._read_audio()
                self._json(self.server.core.recognizer.enroll_voiceprint(audio, content_type))
                return
            if parsed.path == "/api/voiceprint/enroll-last":
                self._json(self.server.core.recognizer.enroll_last_physical_voiceprint())
                return
            json_limit = 1100000 if parsed.path == "/api/files/action" else 65536
            payload = self._read_json(json_limit)
            if parsed.path == "/api/chat":
                self._json(self.server.core.chat(str(payload.get("message", ""))))
                return
            if parsed.path == "/api/dialogue/test":
                self._json(self.server.core.test_dialogue())
                return
            if parsed.path == "/api/monitor":
                message = self.server.core.control_monitor(str(payload.get("action", "")))
                self._json({"message": message, "monitor_running": self.server.core.monitor.is_running()})
                return
            if parsed.path == "/api/body":
                self._json({"body": self.server.core.control_body(payload)})
                return
            if parsed.path == "/api/game":
                action = str(payload.get("action", ""))
                if action == "number_start":
                    result = self.server.core.guess_number.start(
                        range_min=payload.get("range_min"),
                        range_max=payload.get("range_max"),
                    )
                elif action == "number_guess":
                    result = self.server.core.guess_number.guess(payload.get("value"))
                elif action == "rps":
                    result = self.server.core.rps.play(payload.get("choice"))
                    if result["result"] in {"win", "lose"}:
                        try:
                            self.server.core.body.wiggle()
                        except Exception:
                            pass
                else:
                    raise ValueError("不支持的游戏操作")
                self._json(result)
                return
            if parsed.path == "/api/patrol":
                action = str(payload.get("action", ""))
                if action == "start":
                    self.server.core.patrol.start()
                    message = "我开始在桌面慢慢巡逻，碰到东西会绕开。"
                elif action == "stop":
                    self.server.core.patrol.stop()
                    message = "好，我停下，不再巡逻了。"
                elif action == "obstacle":
                    message = self.server.core.patrol.obstacle()
                elif action == "vibration":
                    message = self.server.core.patrol.vibration()
                else:
                    raise ValueError("不支持的巡逻操作")
                self._json(
                    {
                        "message": message,
                        "patrol": self.server.core.patrol.status(),
                    }
                )
                return
            if parsed.path == "/api/computer-access":
                action = str(payload.get("action", ""))
                if action == "set_mode":
                    result = self.server.core.computer_access.set_mode(
                        str(payload.get("mode", "none")),
                        confirmed=payload.get("confirmed") is True,
                        minutes=int(payload.get("minutes", 30)),
                    )
                elif action == "grant_root":
                    result = self.server.core.computer_access.grant_root(
                        str(payload.get("path", "")),
                        confirmed=payload.get("confirmed") is True,
                    )
                elif action == "revoke_root":
                    result = self.server.core.computer_access.revoke_root(
                        str(payload.get("path", "")),
                    )
                elif action == "revoke_all":
                    result = self.server.core.computer_access.revoke()
                else:
                    raise ValueError("不支持的电脑权限操作")
                self._json(result)
                return
            if parsed.path == "/api/files/inspect":
                self._json(self.server.core.computer_access.inspect_file(str(payload.get("path", ""))))
                return
            if parsed.path == "/api/files/action":
                self._json(self.server.core.computer_access.perform_action(payload))
                return
            if parsed.path == "/api/speech":
                realtime_settings = (
                    self.server.core.config.get("device_gateway", {})
                    .get("realtime_voice", {})
                )
                if (
                    bool(realtime_settings.get("enabled", False))
                    and not bool(realtime_settings.get("allow_alternate_voice", False))
                ):
                    raise RuntimeError("网页已固定使用小帮实时音色，不启用其他音色")
                text = str(payload.get("text", ""))
                if self.server.core.speech.streaming_enabled:
                    token = self.server.core.speech.prepare_stream(text)
                    self._json({"audio_url": f"/api/audio-stream/{token}", "streaming": True})
                else:
                    audio = self.server.core.speech.synthesize(text)
                    self._json({"audio_url": f"/api/audio/{audio.name}", "streaming": False})
                return
            if parsed.path == "/api/memories":
                self._json(
                    self.server.core.robot_repository.add_memory(
                        str(payload.get("content", "")),
                        category=str(payload.get("category", "explicit")),
                        importance=int(payload.get("importance", 5)),
                        source="explicit",
                        confirmed=True,
                    )
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        except PermissionError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))
        except (
            json.JSONDecodeError,
            OSError,
            RuntimeError,
            SecretProtectionError,
            sqlite3.Error,
            ValueError,
        ) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/profile":
                self._json({"profile": self.server.core.robot_repository.update_profile(payload)})
                return
            if parsed.path == "/api/dialogue/settings":
                self._json(self.server.core.update_dialogue_settings(payload))
                return
            prefix = "/api/memories/"
            if parsed.path.startswith(prefix):
                memory_id = int(parsed.path[len(prefix):])
                if str(payload.get("action", "")) != "confirm":
                    raise ValueError("不支持的记忆操作")
                self._json({"memory": self.server.core.robot_repository.confirm_memory(memory_id)})
                return
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        except PermissionError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))
        except (json.JSONDecodeError, OSError, SecretProtectionError, sqlite3.Error, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._read_json()
            prefix = "/api/memories/"
            if parsed.path.startswith(prefix):
                memory_id = int(parsed.path[len(prefix):])
                self._json({"deleted": self.server.core.robot_repository.forget_memory(memory_id)})
                return
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        except PermissionError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))
        except (json.JSONDecodeError, OSError, sqlite3.Error, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))


class RobotHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], core: RobotCore, web_root: Path):
        self.core = core
        self.web_root = web_root
        super().__init__(address, RobotRequestHandler)


def load_robot_config(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("robot_config.json 必须是对象")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="桌面机器人核心服务")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = application_root()
    config_path = args.config or root / "robot_config.json"
    config = load_robot_config(config_path)
    runtime = root / config.get("runtime_dir", "runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(runtime / "robot.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logger = logging.getLogger("robot")
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 8765))
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("第一版 Robot Core 只允许监听本机地址")
    core = RobotCore(root, config, logger, config_path)
    gateway_settings = config.get("device_gateway", {})
    gateway = None
    mdns_responder = None
    if bool(gateway_settings.get("enabled", True)):
        device_token = ensure_device_token(root, gateway_settings)
        gateway = XiaozhiWebSocketGateway(
            core,
            host=str(gateway_settings.get("host", "127.0.0.1")),
            port=int(gateway_settings.get("port", 8766)),
            path=str(gateway_settings.get("path", "/xiaobang/v1/")),
            asr_settings=gateway_settings.get("streaming_asr", {}),
            realtime_voice_settings={
                **gateway_settings.get("realtime_voice", {}),
                "conversation_timeout_seconds": gateway_settings.get(
                    "conversation_timeout_seconds", 10
                ),
            },
            api_key_provider=core.dialogue._api_key,
            device_token=device_token,
            logger=logger,
        )
        gateway.start()
        core.device_gateway = gateway
        if bool(gateway_settings.get("mdns_enabled", True)):
            mdns_responder = RobotMdnsResponder(
                hostname=str(
                    gateway_settings.get("mdns_hostname", DEFAULT_MDNS_HOSTNAME)
                ),
                logger=logger,
            )
            mdns_responder.start()
    server = RobotHTTPServer((host, port), core, root / "robot_web")
    url = f"http://127.0.0.1:{port}/"
    logger.info("Robot Core v0.1 已启动：%s", url)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if mdns_responder:
            mdns_responder.stop()
        if gateway:
            gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
