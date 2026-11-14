from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Callable


BODY_PROTOCOL_VERSION = 1
MOTIONS = {"stop", "forward", "backward", "left", "right"}
HEAD_MIN_DEG = 30
HEAD_MAX_DEG = 150
HEAD_CENTER_DEG = 90
EXPRESSIONS = {
    "idle", "listening", "thinking", "speaking", "alert", "happy",
    "curious", "concerned", "relieved", "sleepy", "angry", "offline",
}

EXPRESSION_ALIASES = {
    "normal": "idle",
    "glee": "happy",
    "excited": "happy",
    "focused": "thinking",
    "sad": "concerned",
    "worried": "concerned",
    "scared": "concerned",
    "skeptic": "curious",
    "suspicious": "curious",
    "surprised": "curious",
    "awe": "curious",
    "furious": "alert",
    "annoyed": "angry",
    "frustrated": "angry",
    "unimpressed": "angry",
    "squint": "sleepy",
}

EMOTIONAL_EXPRESSIONS = {
    "happy", "curious", "concerned", "relieved", "sleepy", "alert", "angry",
}
NEUTRAL_EXPRESSIONS = EXPRESSIONS - EMOTIONAL_EXPRESSIONS
STRONG_CONTRAST_PAIRS = {
    frozenset({"happy", "concerned"}),
    frozenset({"happy", "alert"}),
    frozenset({"happy", "angry"}),
    frozenset({"relieved", "concerned"}),
    frozenset({"relieved", "alert"}),
    frozenset({"relieved", "angry"}),
}
TRANSITION_DURATION_MS = {
    "low": 300,
    "medium": 450,
    "strong": 600,
}
SLEEPY_TRANSITION_DURATION_MS = 700
EXIT_TRANSITION_DURATION_MS = 650


IDLE_SWAY_INTERVAL_MIN_MS = 2500
IDLE_SWAY_INTERVAL_MAX_MS = 4500
IDLE_SWAY_DURATION_MIN_MS = 900
IDLE_SWAY_DURATION_MAX_MS = 1300
IDLE_SWAY_AMPLITUDE_MIN_DEG = 2
IDLE_SWAY_AMPLITUDE_MAX_DEG = 5

# 表情与头部/底盘动作的对应规则。全部复用现有舵机、电机能力，
# 不使用任何额外配件。sway 为待机随机轻摆，nod 为说话连续左右轻摆，
# 两者由网页模拟器和实体固件用同一套缓动参数执行。
EXPRESSION_CHOREOGRAPHY = {
    "idle": {
        "kind": "sway",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 0,
    },
    "listening": {
        "kind": "move",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 420,
    },
    "thinking": {
        "kind": "move",
        "pan_deg": 95,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 520,
    },
    "speaking": {
        "kind": "nod",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 0,
        "nod_period_ms": 420,
        "nod_amplitude_deg": 2.0,
        "axis": "pan",
    },
    "happy": {
        "kind": "move",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 480,
    },
    "curious": {
        "kind": "move",
        "pan_deg": 104,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 560,
    },
    "concerned": {
        "kind": "move",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 620,
    },
    "relieved": {
        "kind": "move",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 700,
    },
    "sleepy": {
        "kind": "move",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 760,
        "motion": "forward",
        "speed": 22,
        "motion_duration_ms": 1500,
    },
    "alert": {
        "kind": "move",
        "pan_deg": 97,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 360,
    },
    "angry": {
        "kind": "move",
        "pan_deg": 83,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 420,
    },
    "offline": {
        "kind": "move",
        "pan_deg": HEAD_CENTER_DEG,
        "tilt_deg": HEAD_CENTER_DEG,
        "duration_ms": 700,
    },
}

MANUAL_HEAD_DURATION_MS = 520
CHOREOGRAPHY_NOTIFY_INTERVAL_MS = 50
EASE_OUT_DURATION_FACTOR = 0.62


def normalize_expression(value: object) -> str:
    name = str(value or "").strip().lower()
    name = EXPRESSION_ALIASES.get(name, name)
    return name if name in EXPRESSIONS else "idle"


def is_expression_name_supported(value: object) -> bool:
    name = str(value or "").strip().lower()
    return bool(name) and (name in EXPRESSIONS or name in EXPRESSION_ALIASES)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def expression_contrast_level(from_expression: str, to_expression: str) -> str:
    source = normalize_expression(from_expression)
    target = normalize_expression(to_expression)
    if frozenset({source, target}) in STRONG_CONTRAST_PAIRS:
        return "strong"
    if source in EMOTIONAL_EXPRESSIONS and target in EMOTIONAL_EXPRESSIONS:
        return "medium"
    return "low"


def build_expression_transition(
    from_expression: str,
    to_expression: str,
    *,
    exit_transition: bool = False,
    duration_override: int | None = None,
) -> dict[str, object] | None:
    source = normalize_expression(from_expression)
    target = normalize_expression(to_expression)
    if source == target:
        return None
    level = expression_contrast_level(source, target)
    if target == "sleepy" and level != "strong":
        duration_ms = SLEEPY_TRANSITION_DURATION_MS
    elif exit_transition and target == "idle":
        duration_ms = EXIT_TRANSITION_DURATION_MS
    else:
        duration_ms = int(
            duration_override
            if duration_override is not None
            else TRANSITION_DURATION_MS.get(level, 300)
        )
    duration_ms = max(160, min(duration_ms, 900))

    def phase(expression: str, phase_duration_ms: int) -> dict[str, object]:
        choreo = EXPRESSION_CHOREOGRAPHY.get(
            expression,
            EXPRESSION_CHOREOGRAPHY["idle"],
        )
        return {
            "expression": expression,
            "pan_deg": int(choreo.get("pan_deg", HEAD_CENTER_DEG)),
            "tilt_deg": int(choreo.get("tilt_deg", HEAD_CENTER_DEG)),
            "duration_ms": max(120, phase_duration_ms),
        }

    via = "idle" if level == "strong" else ""
    if via:
        # Strong contrast must finish in exactly 600ms: source -> idle -> target,
        # each phase gets one third of the total transition time.
        third = max(120, duration_ms // 3)
        phases = [phase(source, third), phase(via, third), phase(target, third)]
    else:
        phases = [phase(source, max(120, duration_ms)), phase(target, 0)]
    return {
        "from": source,
        "to": target,
        "via": via,
        "level": level,
        "duration_ms": duration_ms,
        "phases": phases,
    }


class RobotBodyController:
    def __init__(self, settings: dict, logger):
        self.settings = settings
        self.log = logger
        self.enabled = bool(settings.get("enabled", True))
        self.mode = str(settings.get("mode", "simulator"))
        self.max_motion_ms = max(300, min(int(settings.get("max_motion_ms", 2000)), 2000))
        self.pan_min_deg = max(0, min(int(settings.get("pan_min_deg", HEAD_MIN_DEG)), 89))
        self.pan_max_deg = max(
            self.pan_min_deg + 1,
            min(int(settings.get("pan_max_deg", HEAD_MAX_DEG)), 180),
        )
        self.tilt_min_deg = max(0, min(int(settings.get("tilt_min_deg", HEAD_MIN_DEG)), 89))
        self.tilt_max_deg = max(
            self.tilt_min_deg + 1,
            min(int(settings.get("tilt_max_deg", HEAD_MAX_DEG)), 180),
        )
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._head_timer: threading.Timer | None = None
        self._idle_timer: threading.Timer | None = None
        self._wiggle_timer: threading.Timer | None = None
        self._micro_timer: threading.Timer | None = None
        self._wiggle_steps: list[str] = []
        self._wiggle_active = False
        self._wiggle_issuing = False
        self._wiggle_step_ms = 200
        self._wiggle_speed = 30
        self._sequence = 0
        self._action_token = 0
        self._motion_token = 0
        self._motion = "stop"
        self._speed = 0
        self._expression = "idle"
        self._transition: dict[str, object] | None = None
        self._behavior: dict[str, object] = {}
        self._pan_deg = self._clamp_head(
            settings.get("pan_deg", HEAD_CENTER_DEG),
            self.pan_min_deg,
            self.pan_max_deg,
        )
        self._tilt_deg = self._clamp_head(
            settings.get("tilt_deg", HEAD_CENTER_DEG),
            self.tilt_min_deg,
            self.tilt_max_deg,
        )
        self._choreography: dict[str, object] = {}
        self._idle_sway: dict[str, object] = {
            "active": False,
            "count": 0,
            "next_in_ms": 0,
        }
        self._nod_phase = 0
        self._last_command_at = ""
        self._stop_deadline = 0.0
        self._listeners: set[Callable[[dict[str, object]], None]] = set()

    def add_listener(self, listener: Callable[[dict[str, object]], None]) -> None:
        with self._lock:
            self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[dict[str, object]], None]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def _notify(self, snapshot: dict[str, object]) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(dict(snapshot))
            except Exception as error:
                self.log.warning("机器人身体状态广播失败：%s", error)

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _cancel_head_timer_locked(self) -> None:
        if self._head_timer is not None:
            self._head_timer.cancel()
            self._head_timer = None

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _cancel_wiggle_locked(self) -> None:
        if self._wiggle_timer is not None:
            self._wiggle_timer.cancel()
            self._wiggle_timer = None
        self._wiggle_active = False
        self._wiggle_steps = []

    def _cancel_micro_timer_locked(self) -> None:
        if self._micro_timer is not None:
            self._micro_timer.cancel()
            self._micro_timer = None

    def _cancel_all_actions_locked(self) -> None:
        self._cancel_timer_locked()
        self._cancel_head_timer_locked()
        self._cancel_idle_timer_locked()
        self._cancel_wiggle_locked()
        self._cancel_micro_timer_locked()
        self._motion = "stop"
        self._speed = 0
        self._stop_deadline = 0.0
        self._choreography = {}
        self._idle_sway["active"] = False
        self._idle_sway["count"] = 0
        self._idle_sway["next_in_ms"] = 0
        self._nod_phase = 0
        self._transition = None

    def set_behavior_tuning(self, **kwargs: object) -> None:
        with self._lock:
            self._behavior = {
                key: value
                for key, value in kwargs.items()
                if value is not None
            }

    @staticmethod
    def _clamp_head(value: object, minimum: int, maximum: int) -> int:
        try:
            numeric = int(round(float(value)))
        except (TypeError, ValueError):
            numeric = HEAD_CENTER_DEG
        return max(minimum, min(numeric, maximum))

    def _stop_if_current(self, action_token: int) -> None:
        with self._lock:
            if action_token != self._action_token or self._motion == "stop":
                return
            self._sequence += 1
            self._motion = "stop"
            self._speed = 0
            self._stop_deadline = 0.0
            self._last_command_at = _now()
            self._timer = None
            snapshot = self._status_locked()
        self._notify(snapshot)

    def _random_idle_interval_ms(self) -> int:
        return random.randint(IDLE_SWAY_INTERVAL_MIN_MS, IDLE_SWAY_INTERVAL_MAX_MS)

    def _schedule_idle_sway_locked(self) -> None:
        self._cancel_idle_timer_locked()
        if not bool(self._idle_sway.get("active")):
            return
        interval_ms = max(
            IDLE_SWAY_INTERVAL_MIN_MS,
            min(
                int(self._idle_sway.get("next_in_ms", self._random_idle_interval_ms())),
                IDLE_SWAY_INTERVAL_MAX_MS,
            ),
        )
        self._idle_sway["next_in_ms"] = interval_ms
        token = self._action_token
        self._idle_timer = threading.Timer(
            interval_ms / 1000,
            self._trigger_idle_sway,
            args=(token,),
        )
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _trigger_idle_sway(self, token: int) -> None:
        with self._lock:
            if token != self._action_token or not bool(self._idle_sway.get("active")):
                return
            self._sequence += 1
            amplitude = random.uniform(
                IDLE_SWAY_AMPLITUDE_MIN_DEG,
                IDLE_SWAY_AMPLITUDE_MAX_DEG,
            )
            duration_ms = random.randint(
                IDLE_SWAY_DURATION_MIN_MS,
                IDLE_SWAY_DURATION_MAX_MS,
            )
            direction = random.choice((-1, 1))
            target_pan = self._clamp_head(
                HEAD_CENTER_DEG + direction * amplitude,
                self.pan_min_deg,
                self.pan_max_deg,
            )
            self._pan_deg = target_pan
            self._tilt_deg = HEAD_CENTER_DEG
            self._choreography = {
                "kind": "sway",
                "expression": "idle",
                "pan_deg": target_pan,
                "tilt_deg": HEAD_CENTER_DEG,
                "duration_ms": duration_ms,
                "amplitude_deg": round(amplitude, 2),
            }
            self._idle_sway["count"] = int(self._idle_sway.get("count", 0)) + 1
            self._idle_sway["next_in_ms"] = self._random_idle_interval_ms()
            snapshot = self._status_locked()
        self._notify(snapshot)
        with self._lock:
            if token == self._action_token and bool(self._idle_sway.get("active")):
                self._schedule_idle_sway_locked()

    def _schedule_nod_tick_locked(
        self,
        period_ms: int,
        amplitude: float,
        pan_deg: int,
        tilt_deg: int,
        axis: str,
    ) -> None:
        self._cancel_head_timer_locked()
        if self._expression != "speaking" or self._choreography.get("kind") != "nod":
            return
        token = self._action_token
        self._head_timer = threading.Timer(
            period_ms / 2000,
            self._tick_nod,
            args=(token, period_ms, amplitude, pan_deg, tilt_deg, axis),
        )
        self._head_timer.daemon = True
        self._head_timer.start()

    def _tick_nod(
        self,
        token: int,
        period_ms: int,
        amplitude: float,
        pan_deg: int,
        tilt_deg: int,
        axis: str,
    ) -> None:
        with self._lock:
            if (
                token != self._action_token
                or self._expression != "speaking"
                or self._choreography.get("kind") != "nod"
            ):
                return
            self._sequence += 1
            self._nod_phase += 1
            offset = amplitude if self._nod_phase % 2 == 1 else -amplitude
            if axis == "pan":
                target_pan = self._clamp_head(
                    pan_deg + offset,
                    self.pan_min_deg,
                    self.pan_max_deg,
                )
                target_tilt = self._clamp_head(
                    tilt_deg,
                    self.tilt_min_deg,
                    self.tilt_max_deg,
                )
                self._pan_deg = target_pan
                self._tilt_deg = target_tilt
                self._choreography["pan_deg"] = target_pan
                self._choreography["tilt_deg"] = target_tilt
            else:
                target_tilt = self._clamp_head(
                    tilt_deg + offset,
                    self.tilt_min_deg,
                    self.tilt_max_deg,
                )
                self._pan_deg = pan_deg
                self._tilt_deg = target_tilt
                self._choreography["tilt_deg"] = target_tilt
            self._choreography["duration_ms"] = max(160, period_ms // 2)
            snapshot = self._status_locked()
        self._notify(snapshot)
        with self._lock:
            if (
                token == self._action_token
                and self._expression == "speaking"
                and self._choreography.get("kind") == "nod"
            ):
                self._schedule_nod_tick_locked(
                    period_ms,
                    amplitude,
                    pan_deg,
                    tilt_deg,
                    axis,
                )

    def _start_expression_choreography_locked(
        self,
        expression: str,
        *,
        nod_period_ms: int | None = None,
        motion_already_set: bool = False,
        emotion_intensity: float = 1.0,
        follow_up: bool = False,
    ) -> None:
        choreo = EXPRESSION_CHOREOGRAPHY.get(expression, EXPRESSION_CHOREOGRAPHY["idle"])
        kind = str(choreo.get("kind", "move"))
        duration_ms = max(0, int(choreo.get("duration_ms", 0) or 0))
        target_pan = self._clamp_head(
            98 if (expression == "thinking" and follow_up) else choreo.get("pan_deg", HEAD_CENTER_DEG),
            self.pan_min_deg,
            self.pan_max_deg,
        )
        target_tilt = self._clamp_head(
            choreo.get("tilt_deg", HEAD_CENTER_DEG),
            self.tilt_min_deg,
            self.tilt_max_deg,
        )
        axis = str(choreo.get("axis", "tilt"))
        if expression == "thinking" and follow_up:
            duration_ms = 620
        intensity = max(0.5, min(float(emotion_intensity), 1.6))
        behavior = self._behavior
        energy = max(0.4, min(float(behavior.get("energy", 1.0) or 1.0), 1.5))
        if bool(behavior.get("quiet")):
            energy = min(energy, 0.7)
        if bool(behavior.get("low_battery")):
            energy = min(energy, 0.65)
        if kind == "nod":
            duration_ms = max(160, int(duration_ms * energy * intensity))
        elif duration_ms > 0:
            duration_ms = max(200, int(duration_ms * energy * intensity))
        self._choreography = {
            "kind": kind,
            "expression": expression,
            "pan_deg": target_pan,
            "tilt_deg": target_tilt,
            "duration_ms": duration_ms,
            "axis": axis,
        }
        if kind == "nod":
            period = max(
                260,
                min(
                    int(nod_period_ms if nod_period_ms is not None else choreo.get("nod_period_ms", 420)),
                    800,
                ),
            )
            amplitude = max(
                0.5,
                min(
                    float(choreo.get("nod_amplitude_deg", 2.0)) * intensity * energy,
                    3.0,
                ),
            )
            self._pan_deg = target_pan
            self._tilt_deg = target_tilt
            self._choreography["nod_period_ms"] = period
            self._choreography["nod_amplitude_deg"] = amplitude
            self._nod_phase = 0
            self._schedule_nod_tick_locked(
                period,
                amplitude,
                target_pan,
                target_tilt,
                axis,
            )
            return
        if kind == "sway":
            self._pan_deg = target_pan
            self._tilt_deg = target_tilt
            self._idle_sway["active"] = True
            self._idle_sway["count"] = 0
            self._idle_sway["next_in_ms"] = self._random_idle_interval_ms()
            self._schedule_idle_sway_locked()
            return
        self._pan_deg = target_pan
        self._tilt_deg = target_tilt
        motion = str(choreo.get("motion", "")) if choreo.get("motion") else ""
        if motion in MOTIONS and motion != "stop" and not motion_already_set:
            self._motion = motion
            self._speed = max(20, min(int(choreo.get("speed", 22)), 70))
            motion_ms = max(
                200,
                min(int(choreo.get("motion_duration_ms", 1500)), self.max_motion_ms),
            )
            self._stop_deadline = time.monotonic() + motion_ms / 1000
            motion_token = self._action_token
            self._timer = threading.Timer(
                motion_ms / 1000,
                self._stop_if_current,
                args=(motion_token,),
            )
            self._timer.daemon = True
            self._timer.start()
            self._choreography["motion"] = motion
            self._choreography["speed"] = self._speed
            self._choreography["motion_duration_ms"] = motion_ms

    def _start_manual_head_locked(self) -> None:
        self._choreography = {
            "kind": "move",
            "expression": self._expression,
            "pan_deg": self._pan_deg,
            "tilt_deg": self._tilt_deg,
            "duration_ms": MANUAL_HEAD_DURATION_MS,
        }

    def _advance_micro_phases(self, token: int) -> None:
        with self._lock:
            if token != self._action_token or self._choreography.get("kind") != "micro":
                return
            phases = list(self._choreography.get("phases", []))
            if not phases:
                self._choreography = {}
                self._micro_timer = None
                snapshot = self._status_locked()
            else:
                current = phases.pop(0)
                self._pan_deg = self._clamp_head(
                    current.get("pan_deg", self._pan_deg),
                    self.pan_min_deg,
                    self.pan_max_deg,
                )
                self._tilt_deg = self._clamp_head(
                    current.get("tilt_deg", self._tilt_deg),
                    self.tilt_min_deg,
                    self.tilt_max_deg,
                )
                self._choreography["phases"] = phases
                self._sequence += 1
                delay_ms = max(120, int(current.get("duration_ms", 0)))
                if phases:
                    snapshot = self._status_locked()
                    self._micro_timer = threading.Timer(
                        delay_ms / 1000,
                        self._advance_micro_phases,
                        args=(token,),
                    )
                    self._micro_timer.daemon = True
                    self._micro_timer.start()
                else:
                    self._choreography = {}
                    self._micro_timer = None
                    snapshot = self._status_locked()
        self._notify(snapshot)

    def command(
        self,
        *,
        motion: str | None = None,
        expression: str | None = None,
        pan_deg: int | float | None = None,
        tilt_deg: int | float | None = None,
        speed: int = 45,
        duration_ms: int = 800,
        nod_period_ms: int | None = None,
        micro_action: str | None = None,
        emotion_intensity: float = 1.0,
        follow_up: bool = False,
        exit_transition: bool = False,
    ) -> dict[str, object]:
        if not self.enabled:
            raise RuntimeError("机器人身体控制未启用")
        if motion is None and expression is None and pan_deg is None and tilt_deg is None:
            if micro_action not in {"gap", "wander"}:
                raise ValueError("身体命令不能为空")
        if motion is not None and motion not in MOTIONS:
            raise ValueError("不支持的移动命令")
        if expression is not None and not is_expression_name_supported(expression):
            raise ValueError("不支持的表情命令")
        with self._lock:
            previous_expression = self._expression
            self._sequence += 1
            self._last_command_at = _now()
            if not self._wiggle_issuing:
                self._action_token += 1
                self._cancel_all_actions_locked()
            else:
                # Wiggle steps keep the wiggle state alive, but each new step
                # must invalidate the previous motion timer so it cannot stop
                # the next step early.
                self._action_token += 1
                self._cancel_timer_locked()
            transition = None
            micro_phases: list[dict[str, object]] = []
            if motion is not None:
                self._motion_token += 1
                self._motion = motion
                if motion == "stop":
                    self._speed = 0
                    self._stop_deadline = 0.0
                else:
                    self._speed = max(20, min(int(speed), 70))
                    safe_duration = max(200, min(int(duration_ms), self.max_motion_ms))
                    self._stop_deadline = time.monotonic() + safe_duration / 1000
                    motion_token = self._action_token
                    self._timer = threading.Timer(
                        safe_duration / 1000,
                        self._stop_if_current,
                        args=(motion_token,),
                    )
                    self._timer.daemon = True
                    self._timer.start()
            if micro_action == "gap":
                offset = random.choice((3, -3))
                micro_phases = [
                    {
                        "expression": self._expression,
                        "pan_deg": self._clamp_head(
                            self._pan_deg + offset,
                            self.pan_min_deg,
                            self.pan_max_deg,
                        ),
                        "tilt_deg": self._tilt_deg,
                        "duration_ms": 360,
                    },
                    {
                        "expression": self._expression,
                        "pan_deg": self._pan_deg,
                        "tilt_deg": self._tilt_deg,
                        "duration_ms": 480,
                    },
                ]
            elif micro_action == "wander":
                micro_phases = [
                    {
                        "expression": self._expression,
                        "pan_deg": self._clamp_head(
                            HEAD_CENTER_DEG - 10,
                            self.pan_min_deg,
                            self.pan_max_deg,
                        ),
                        "tilt_deg": HEAD_CENTER_DEG,
                        "duration_ms": 900,
                    },
                    {
                        "expression": self._expression,
                        "pan_deg": HEAD_CENTER_DEG,
                        "tilt_deg": HEAD_CENTER_DEG,
                        "duration_ms": 900,
                    },
                ]
            if micro_phases:
                current = micro_phases.pop(0)
                self._pan_deg = self._clamp_head(
                    current.get("pan_deg", self._pan_deg),
                    self.pan_min_deg,
                    self.pan_max_deg,
                )
                self._tilt_deg = self._clamp_head(
                    current.get("tilt_deg", self._tilt_deg),
                    self.tilt_min_deg,
                    self.tilt_max_deg,
                )
                self._choreography = {
                    "kind": "micro",
                    "expression": self._expression,
                    "phases": micro_phases,
                    "duration_ms": int(current.get("duration_ms", 0))
                    + sum(int(phase.get("duration_ms", 0)) for phase in micro_phases),
                }
                token = self._action_token
                delay_ms = max(120, int(current.get("duration_ms", 0)))
                self._micro_timer = threading.Timer(
                    delay_ms / 1000,
                    self._advance_micro_phases,
                    args=(token,),
                )
                self._micro_timer.daemon = True
                self._micro_timer.start()
            if expression is not None:
                normalized = normalize_expression(expression)
                transition = build_expression_transition(
                    previous_expression,
                    normalized,
                    exit_transition=exit_transition,
                )
                self._transition = transition
                self._expression = normalized
                self._start_expression_choreography_locked(
                    normalized,
                    nod_period_ms=nod_period_ms,
                    motion_already_set=motion is not None,
                    emotion_intensity=emotion_intensity,
                    follow_up=follow_up,
                )
            if pan_deg is not None or tilt_deg is not None:
                if pan_deg is not None:
                    self._pan_deg = self._clamp_head(
                        pan_deg,
                        self.pan_min_deg,
                        self.pan_max_deg,
                    )
                if tilt_deg is not None:
                    self._tilt_deg = self._clamp_head(
                        tilt_deg,
                        self.tilt_min_deg,
                        self.tilt_max_deg,
                    )
                self._start_manual_head_locked()
                self._transition = None
            snapshot = self._status_locked()
        self._notify(snapshot)
        return snapshot

    def wiggle(
        self,
        *,
        duration_ms: int = 700,
        speed: int = 30,
    ) -> dict[str, object]:
        """Play a short left-right-left shake using low-speed motions."""
        if not self.enabled:
            raise RuntimeError("机器人身体控制未启用")
        step_ms = max(220, min(max(260, int(duration_ms)) // 3, 480))
        speed_value = max(20, min(int(speed), 30))
        with self._lock:
            self._action_token += 1
            self._cancel_all_actions_locked()
            self._sequence += 1
            self._last_command_at = _now()
            self._wiggle_active = True
            self._wiggle_steps = ["left", "right", "left"]
            self._wiggle_step_ms = step_ms
            self._wiggle_speed = speed_value
        self._advance_wiggle()
        return self.status()

    def _advance_wiggle(self) -> None:
        with self._lock:
            if not self._wiggle_active or not self._wiggle_steps:
                return
            motion = self._wiggle_steps.pop(0)
            step_ms = self._wiggle_step_ms
            speed = self._wiggle_speed
            self._wiggle_issuing = True
        try:
            self.command(motion=motion, speed=speed, duration_ms=step_ms)
        finally:
            with self._lock:
                self._wiggle_issuing = False
        with self._lock:
            if not self._wiggle_steps:
                self._wiggle_active = False
                self._wiggle_timer = None
                return
            if not self._wiggle_active:
                return
            timer = threading.Timer(step_ms / 1000, self._advance_wiggle)
            timer.daemon = True
            self._wiggle_timer = timer
        timer.start()

    def _status_locked(self) -> dict[str, object]:
        remaining_ms = max(0, round((self._stop_deadline - time.monotonic()) * 1000))
        choreography = dict(self._choreography) if self._choreography else None
        transition = (
            {
                key: value
                for key, value in self._transition.items()
                if key != "phases" or self._transition.get("phases")
            }
            if self._transition
            else None
        )
        idle_sway = None
        if bool(self._idle_sway.get("active")):
            idle_sway = {
                "active": True,
                "count": int(self._idle_sway.get("count", 0)),
                "next_in_ms": max(0, int(self._idle_sway.get("next_in_ms", 0))),
            }
        return {
            "protocol_version": BODY_PROTOCOL_VERSION,
            "enabled": self.enabled,
            "mode": self.mode,
            "connected": self.enabled and self.mode == "simulator",
            "sequence": self._sequence,
            "motion": self._motion,
            "speed": self._speed,
            "expression": self._expression,
            "pan_deg": self._pan_deg,
            "tilt_deg": self._tilt_deg,
            "remaining_ms": remaining_ms,
            "choreography": choreography,
            "idle_sway": idle_sway,
            "transition": transition,
            "behavior": dict(self._behavior),
            "last_command_at": self._last_command_at,
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            return self._status_locked()
