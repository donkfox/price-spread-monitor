from __future__ import annotations

import random
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _now() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def _normalize_fragment(content: str) -> str:
    return re.sub(r"[\W_]+", "", content, flags=re.UNICODE).lower()


FRAGMENT_SENSITIVE_MARKERS = (
    "密码", "验证码", "支付密码", "银行卡", "身份证", "护照号",
    "家庭住址", "详细地址", "私钥", "api key", "apikey", "token",
)
FRAGMENT_COMMAND_MARKERS = (
    "开始监控", "开启监控", "启动监控", "停止监控", "关闭监控",
    "监控状态", "本月利润", "这个月利润", "查看记忆", "打开文件",
    "开始巡逻", "停止巡逻", "开始猜数字", "石头剪刀布", "桌面震动",
)
FRAGMENT_SKIP_MARKERS = (
    "你好", "您好", "早上好", "晚上好", "哈喽", "嗨", "拜拜",
    "再见", "晚安", "谢谢", "开始", "停止", "关闭", "退出",
)

PROACTIVE_MIN_IDLE_MINUTES = 45
PROACTIVE_MIN_INTERVAL_MINUTES = 45
PROACTIVE_DAILY_LIMIT = 5
PROACTIVE_MAX_CONSECUTIVE_MISSED = 2
PROACTIVE_PAUSE_MINUTES = 240


class CompanionManager:
    """Persistent short-term companion mood and daily conversation fragments."""

    def __init__(self, database: Path, logger) -> None:
        self.database = Path(database)
        self.log = logger
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.proactive = ProactiveCompanion(self)

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

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS companion_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topic_fragments (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    last_mentioned_at TEXT NOT NULL DEFAULT '',
                    mention_count INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                VALUES(?, ?, ?)
                """,
                ("playfulness", "35", _now()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                VALUES(?, ?, ?)
                """,
                ("last_interaction_at", _now(), _now()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                VALUES(?, ?, ?)
                """,
                ("last_playful_at", "", _now()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                VALUES(?, ?, ?)
                """,
                ("interaction_count", "0", _now()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                VALUES(?, ?, ?)
                """,
                ("mood_balance", "0", _now()),
            )
            for key in (
                "proactive_day",
                "proactive_last_at",
                "proactive_responded_at",
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                    VALUES(?, ?, ?)
                    """,
                    (key, "", _now()),
                )
            for key in ("proactive_daily_count", "proactive_missed_count"):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                    VALUES(?, ?, ?)
                    """,
                    (key, "0", _now()),
                )
            trait_row = connection.execute(
                "SELECT value FROM companion_state WHERE key='personality_trait'"
            ).fetchone()
            if trait_row is None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO companion_state(key, value, updated_at)
                    VALUES(?, ?, ?)
                    """,
                    ("personality_trait", random.choice(("内敛", "轻快")), _now()),
                )

    def _get_state(self, key: str, default: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM companion_state WHERE key=?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def _set_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO companion_state(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, _now()),
            )

    def touch(self, playful: bool = False) -> int:
        now = _beijing_now()
        playfulness = int(self._get_state("playfulness", "35"))
        last_raw = self._get_state("last_interaction_at", "")
        decay = 0
        mood = int(self._get_state("mood_balance", "0"))
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
                idle_hours = max(0.0, (now - last).total_seconds() / 3600)
                decay = min(25, int(idle_hours / 4) * 4)
                if idle_hours > 0:
                    mood = max(
                        -100,
                        min(100, int(round(mood * max(0.0, 1.0 - idle_hours / 24.0)))),
                    )
                    self._set_state("mood_balance", str(mood))
            except ValueError:
                decay = 0
        playfulness = max(0, min(100, playfulness - decay + (8 if playful else 2)))
        self._set_state("playfulness", str(playfulness))
        self._set_state("last_interaction_at", _now())
        interaction_count = int(self._get_state("interaction_count", "0")) + 1
        self._set_state("interaction_count", str(interaction_count))
        if playful:
            self._set_state("last_playful_at", _now())
        return playfulness

    @staticmethod
    def _mood_delta(emotion: str) -> int:
        name = str(emotion or "").strip().lower()
        deltas = {
            "happy": 6,
            "excited": 6,
            "relieved": 5,
            "curious": 2,
            "idle": 1,
            "listening": 1,
            "thinking": 0,
            "speaking": 0,
            "sleepy": -1,
            "alert": -4,
            "angry": -4,
            "concerned": -5,
        }
        return deltas.get(name, 0)

    def record_emotion(self, emotion: str) -> int:
        mood = int(self._get_state("mood_balance", "0"))
        mood = max(-100, min(100, mood + self._mood_delta(emotion)))
        self._set_state("mood_balance", str(mood))
        return mood

    def observe_message(self, message: str) -> dict[str, object] | None:
        value = " ".join(message.strip().split())
        if not value:
            return None
        lower = value.lower()
        compact = "".join(value.split())
        if any(marker in lower for marker in FRAGMENT_SENSITIVE_MARKERS):
            return None
        if any(marker in compact for marker in FRAGMENT_COMMAND_MARKERS):
            return None
        if any(marker in compact for marker in FRAGMENT_SKIP_MARKERS):
            return None
        if value.startswith("记住") or value.endswith(("?", "？", "吗", "吗？", "呢", "呢？")):
            return None
        content = value[:120]
        topic_key = _normalize_fragment(content)
        if len(topic_key) < 3:
            return None
        self.touch(playful=bool(
            any(word in compact for word in ("逗你", "你真有意思", "好可爱", "哈哈", "嘿嘿", "来玩", "转个圈", "跳个舞", "笨", "傻"))
        ))
        created_at = _now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, mention_count FROM topic_fragments
                WHERE topic_key=? ORDER BY id DESC LIMIT 1
                """,
                (topic_key,),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE topic_fragments
                    SET mention_count=?, last_mentioned_at=?
                    WHERE id=?
                    """,
                    (int(existing["mention_count"]) + 1, created_at, int(existing["id"])),
                )
                return dict(existing)
            cursor = connection.execute(
                """
                INSERT INTO topic_fragments(
                    created_at, content, topic_key, last_mentioned_at, mention_count
                ) VALUES(?, ?, ?, '', 1)
                """,
                (created_at, content, topic_key),
            )
            return {"id": int(cursor.lastrowid), "created_at": created_at, "content": content}

    def recent_fragments(self, limit: int = 8) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, content, mention_count, last_mentioned_at
                FROM topic_fragments
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(limit, 50)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def fragment_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM topic_fragments"
            ).fetchone()
        return int(row["count"])

    def _relative_day(self, created_at: str, today: datetime.date) -> str:
        try:
            created = datetime.fromisoformat(created_at).date()
        except ValueError:
            return "之前"
        delta = (today - created).days
        if delta == 0:
            return "今天"
        if delta == 1:
            return "昨天"
        return f"{delta}天前"

    def suggest_topic(self, *, mark: bool = False) -> dict[str, object] | None:
        today = _beijing_now().date().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, content, mention_count, last_mentioned_at
                FROM topic_fragments
                WHERE substr(created_at, 1, 10) < ?
                  AND (last_mentioned_at='' OR substr(last_mentioned_at, 1, 10) < ?)
                ORDER BY created_at DESC, id DESC LIMIT 5
                """,
                (today, today),
            ).fetchall()
        if not rows:
            return None
        row = dict(rows[0])
        relative = self._relative_day(str(row["created_at"]), _beijing_now().date())
        suggestion = f"我记得{relative}你提到“{row['content']}”，今天还要继续聊这个吗？"
        if mark:
            self._mark_mentioned(int(row["id"]))
        return {
            **row,
            "relative_day": relative,
            "suggestion": suggestion,
        }

    def _mark_mentioned(self, fragment_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE topic_fragments SET last_mentioned_at=?
                WHERE id=?
                """,
                (_now(), fragment_id),
            )

    def recall_topic(self) -> dict[str, object] | None:
        return self.suggest_topic(mark=True)

    def recent_topic_line(self) -> str:
        fragments = self.recent_fragments(3)
        if not fragments:
            return "- 暂无"
        return "\n".join(
            f"- {str(item['content']).strip()[:80]}"
            for item in fragments
            if str(item.get("content", "")).strip()
        )

    def summary(self) -> dict[str, object]:
        playfulness = int(self._get_state("playfulness", "35"))
        interaction_count = int(self._get_state("interaction_count", "0"))
        personality_trait = str(self._get_state("personality_trait", "内敛"))
        mood_balance = int(self._get_state("mood_balance", "0"))
        last_raw = self._get_state("last_interaction_at", "")
        idle_minutes = 0
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
                idle_minutes = max(0, int((_beijing_now() - last).total_seconds() // 60))
            except ValueError:
                idle_minutes = 0
        sleepy = idle_minutes >= 20
        if playfulness >= 65:
            personality_mode = "活泼"
        elif sleepy:
            personality_mode = "困倦"
        else:
            personality_mode = "平静"
        if sleepy:
            emotion_style = "安静、简短，带着一点困意"
        elif playfulness >= 65:
            emotion_style = "活泼、轻松，会主动接住玩笑"
        elif playfulness >= 40:
            emotion_style = "温和、自然，保持亲近"
        else:
            emotion_style = "沉稳、简洁，话不多但认真"
        if interaction_count >= 60:
            familiarity = "很熟悉"
        elif interaction_count >= 20:
            familiarity = "熟悉"
        else:
            familiarity = "陌生"
        fragments = self.recent_fragments(5)
        return {
            "playfulness": playfulness,
            "interaction_count": interaction_count,
            "personality_trait": personality_trait,
            "mood_balance": mood_balance,
            "familiarity": familiarity,
            "personality_mode": personality_mode,
            "emotion_style": emotion_style,
            "last_interaction_at": last_raw,
            "idle_minutes": idle_minutes,
            "standby": {
                "sleepy": sleepy,
                "note": "眼皮有点耷拉，偶尔小声嘟囔" if sleepy else "清醒待机",
            },
            "fragment_count": self.fragment_count(),
            "recent_fragments": fragments,
            "topic_suggestion": self.suggest_topic(),
            "proactive": self.proactive.overview(),
        }

    def emotion_style(self) -> str:
        return str(self.summary()["emotion_style"])


class ProactiveCompanion:
    """Conservative decision gate for occasional proactive greetings.

    This only answers whether a proactive greeting is eligible; it never
    starts its own thread or timer, so it cannot pile up background tasks.
    """

    def __init__(self, manager: CompanionManager) -> None:
        self.manager = manager

    def _idle_minutes(self, now: datetime) -> int:
        last_raw = self.manager._get_state("last_interaction_at", "")
        if not last_raw:
            return 0
        try:
            last = datetime.fromisoformat(last_raw)
        except ValueError:
            return 0
        return max(0, int((now - last).total_seconds() // 60))

    def _daily_count(self, now: datetime) -> int:
        today = now.date().isoformat()
        if self.manager._get_state("proactive_day", "") != today:
            return 0
        return int(self.manager._get_state("proactive_daily_count", "0"))

    def can_initiate(
        self,
        *,
        busy: bool = False,
        low_battery: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        current = now or _beijing_now()
        idle_minutes = self._idle_minutes(current)
        if busy:
            return self._decision(False, "正在对话", idle_minutes, current)
        if low_battery:
            return self._decision(False, "低电量", idle_minutes, current)
        if idle_minutes < PROACTIVE_MIN_IDLE_MINUTES:
            return self._decision(False, "待机时间不足", idle_minutes, current)
        if self._daily_count(current) >= PROACTIVE_DAILY_LIMIT:
            return self._decision(False, "今日已达上限", idle_minutes, current)
        if (
            int(self.manager._get_state("proactive_missed_count", "0"))
            >= PROACTIVE_MAX_CONSECUTIVE_MISSED
        ):
            return self._decision(False, "连续未回应，暂停", idle_minutes, current)
        last_raw = self.manager._get_state("proactive_last_at", "")
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
                if (current - last).total_seconds() / 60 < PROACTIVE_MIN_INTERVAL_MINUTES:
                    return self._decision(False, "间隔不足", idle_minutes, current)
            except ValueError:
                pass
        return self._decision(True, "可以发起", idle_minutes, current)

    def _decision(
        self,
        allowed: bool,
        reason: str,
        idle_minutes: int,
        now: datetime,
    ) -> dict[str, object]:
        return {
            "allowed": allowed,
            "reason": reason,
            "idle_minutes": idle_minutes,
            "min_idle_minutes": PROACTIVE_MIN_IDLE_MINUTES,
            "daily_limit": PROACTIVE_DAILY_LIMIT,
            "daily_count": self._daily_count(now),
            "missed_count": int(self.manager._get_state("proactive_missed_count", "0")),
        }

    def record_initiated(self, now: datetime | None = None) -> None:
        current = now or _beijing_now()
        today = current.date().isoformat()
        count = self._daily_count(current) + 1
        self.manager._set_state("proactive_last_at", current.isoformat(timespec="seconds"))
        self.manager._set_state("proactive_day", today)
        self.manager._set_state("proactive_daily_count", str(count))

    def record_responded(self) -> None:
        self.manager._set_state("proactive_missed_count", "0")
        self.manager._set_state("proactive_responded_at", _now())

    def record_missed(self) -> None:
        missed = int(self.manager._get_state("proactive_missed_count", "0")) + 1
        self.manager._set_state("proactive_missed_count", str(missed))

    def overview(self) -> dict[str, object]:
        decision = self.can_initiate()
        return {
            "allowed": bool(decision["allowed"]),
            "reason": str(decision["reason"]),
            "daily_count": int(decision["daily_count"]),
            "missed_count": int(decision["missed_count"]),
            "min_idle_minutes": PROACTIVE_MIN_IDLE_MINUTES,
            "daily_limit": PROACTIVE_DAILY_LIMIT,
        }


class GuessNumberGame:
    def __init__(self, range_min: int = 1, range_max: int = 10) -> None:
        self.range_min = range_min
        self.range_max = range_max
        self.active = False
        self.target: int | None = None
        self.attempts = 0
        self.last_result = ""

    def start(
        self,
        range_min: int | None = None,
        range_max: int | None = None,
    ) -> dict[str, object]:
        minimum = int(range_min if range_min is not None else self.range_min)
        maximum = int(range_max if range_max is not None else self.range_max)
        if minimum < 1 or maximum > 1000 or minimum >= maximum:
            raise ValueError("猜数字范围要在 1 到 1000 之间")
        self.range_min = minimum
        self.range_max = maximum
        self.target = random.randint(minimum, maximum)
        self.attempts = 0
        self.active = True
        self.last_result = "start"
        return {
            "action": "number_start",
            "active": True,
            "range_min": minimum,
            "range_max": maximum,
            "attempts": 0,
            "reply": f"好，我出一个 {minimum} 到 {maximum} 的数字，你猜几次能猜中？",
            "emotion": "curious",
        }

    def guess(self, value: object) -> dict[str, object]:
        if not self.active or self.target is None:
            raise ValueError("还没开始猜数字，先说“开始猜数字”。")
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("猜的数字无效") from error
        if number < self.range_min or number > self.range_max:
            raise ValueError(f"数字要在 {self.range_min} 到 {self.range_max} 之间")
        self.attempts += 1
        if number < self.target:
            result = "low"
            reply = f"太小了，再来一次，这是第 {self.attempts} 次。"
            emotion = "curious"
        elif number > self.target:
            result = "high"
            reply = f"太大了，再来一次，这是第 {self.attempts} 次。"
            emotion = "curious"
        else:
            result = "correct"
            reply = f"猜对了，就是 {self.target}，你用了 {self.attempts} 次。"
            emotion = "happy"
            self.active = False
        self.last_result = result
        payload: dict[str, object] = {
            "action": "number_guess",
            "active": self.active,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "attempts": self.attempts,
            "result": result,
            "reply": reply,
            "emotion": emotion,
        }
        if result == "correct":
            payload["target"] = self.target
        return payload

    def state(self) -> dict[str, object]:
        return {
            "active": self.active,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "attempts": self.attempts,
            "last_result": self.last_result,
        }


class RockPaperScissors:
    CHOICES = {
        "rock": "石头",
        "paper": "布",
        "scissors": "剪刀",
    }
    ALIASES = {
        "rock": "rock",
        "paper": "paper",
        "scissors": "scissors",
        "石头": "rock",
        "锤子": "rock",
        "布": "paper",
        "剪刀": "scissors",
        "剪子": "scissors",
    }

    def __init__(self) -> None:
        self.last_result = ""

    def play(self, choice: object) -> dict[str, object]:
        raw = str(choice or "").strip().lower()
        player = self.ALIASES.get(raw)
        if player is None:
            raise ValueError("要说石头、剪刀或布")
        bot = random.choice(list(self.CHOICES))
        if player == bot:
            result = "draw"
            reply = f"你出{self.CHOICES[player]}，我出{self.CHOICES[bot]}。平局，再来一次。"
            emotion = "curious"
        elif (player, bot) in {("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")}:
            result = "win"
            reply = f"你出{self.CHOICES[player]}，我出{self.CHOICES[bot]}。你赢了这一局，反应真快。"
            emotion = "happy"
        else:
            result = "lose"
            reply = f"你出{self.CHOICES[player]}，我出{self.CHOICES[bot]}。这局我赢了，再来一次。"
            emotion = "relieved"
        self.last_result = result
        return {
            "action": "rps",
            "player_choice": player,
            "player_label": self.CHOICES[player],
            "bot_choice": bot,
            "bot_label": self.CHOICES[bot],
            "result": result,
            "reply": reply,
            "emotion": emotion,
        }

    def state(self) -> dict[str, object]:
        return {"last_result": self.last_result}


class PatrolController:
    """Simulated desktop patrol with obstacle and vibration reactions."""

    def __init__(self, body_command: Callable[..., object], logger) -> None:
        self._body_command = body_command
        self.log = logger
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._timers: list[threading.Timer] = []
        self._wake = threading.Event()
        self._event_generation = 0
        self._event_active = False
        self.last_event = ""
        self.started_at = ""

    def _safe_command(self, **kwargs) -> object | None:
        try:
            return self._body_command(**kwargs)
        except Exception as error:
            self.log.warning("巡逻身体命令失败：%s", error)
            return None

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._running:
                return self.status()
            self._running = True
            self._event_generation += 1
            self._event_active = False
            self.started_at = _now()
            self.last_event = "start"
            self._wake.clear()
        self._safe_command(motion="left", speed=35, duration_ms=900)
        thread = threading.Thread(
            target=self._run,
            name="robot-patrol",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return self.status()

    def stop(self) -> dict[str, object]:
        with self._lock:
            self._running = False
            self._event_generation += 1
            self._event_active = False
            self._wake.set()
            timers = list(self._timers)
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        self._safe_command(motion="stop")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.8)
        self.last_event = "stop"
        return self.status()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
            self._wake.wait(1.3)
            with self._lock:
                if not self._running:
                    return
                if self._event_active:
                    continue
            self._safe_command(motion="left", speed=35, duration_ms=900)

    def _schedule(self, function: Callable[[], None], delay: float) -> None:
        generation = self._event_generation
        holder: dict[str, object] = {}

        def run_generation_guarded() -> None:
            current = holder.get("timer")
            try:
                self._run_generation_guarded(generation, function)
            finally:
                if current is not None:
                    with self._lock:
                        try:
                            self._timers.remove(current)
                        except ValueError:
                            pass

        timer = threading.Timer(
            delay,
            run_generation_guarded,
        )
        holder["timer"] = timer
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()

    def _run_generation_guarded(
        self,
        generation: int,
        function: Callable[[], None],
    ) -> None:
        with self._lock:
            if generation != self._event_generation:
                return
        function()

    def _resume(self) -> None:
        with self._lock:
            self._event_active = False
            if not self._running:
                return
        self._safe_command(motion="left", speed=35, duration_ms=900)

    def obstacle(self) -> str:
        with self._lock:
            self._event_generation += 1
            self._event_active = True
        self._safe_command(motion="stop", expression="curious")
        self._schedule(
            lambda: self._safe_command(motion="right", speed=35, duration_ms=450),
            0.35,
        )
        self._schedule(self._resume, 0.85)
        self.last_event = "obstacle"
        return "前面好像有障碍，我停一下绕开。"

    def vibration(self) -> str:
        with self._lock:
            self._event_generation += 1
            self._event_active = True
        self._safe_command(motion="stop", expression="alert")
        self._schedule(lambda: self._safe_command(pan_deg=110), 0.30)
        self._schedule(lambda: self._safe_command(pan_deg=70), 0.60)
        self._schedule(lambda: self._safe_command(pan_deg=90), 0.90)
        self._schedule(self._resume, 1.20)
        self.last_event = "vibration"
        return "我感觉到桌面震了一下，先停下来警觉看看。"

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": self._running,
                "mode": "patrol" if self._running else "standby",
                "last_event": self.last_event,
                "started_at": self.started_at,
            }
