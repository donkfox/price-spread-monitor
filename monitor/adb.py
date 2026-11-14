from __future__ import annotations

import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Bounds
from .ui import UiTree


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AdbError(RuntimeError):
    pass


class RiskDetected(RuntimeError):
    pass


class AuthenticationRequired(RuntimeError):
    pass


class Adb:
    FAST_DUMP_REMOTE_JAR = "/data/local/tmp/price-monitor-fast-dump.jar"
    FAST_DUMP_REMOTE_XML = "/sdcard/price-monitor-window.xml"

    def __init__(self, executable: Path, serial: str, runtime_dir: Path):
        self.executable = executable
        self.serial = serial
        self.runtime_dir = runtime_dir
        self._fast_dump_pushed = False
        runtime_dir.mkdir(parents=True, exist_ok=True)

    def run(self, *args: str, timeout: float = 20, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [str(self.executable), "-s", self.serial, *args]
        for attempt in range(2):
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    creationflags=CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired as error:
                if attempt == 0:
                    self._reconnect_after_timeout()
                    continue
                raise AdbError(
                    f"ADB command timed out after {timeout:g}s: {' '.join(command)}"
                ) from error
            output = f"{result.stdout}\n{result.stderr}".lower()
            if result.returncode == 0 or not self._is_transient_connection_error(output):
                break
            if attempt == 0:
                subprocess.run(
                    [str(self.executable), "connect", self.serial],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                    creationflags=CREATE_NO_WINDOW,
                )
                time.sleep(0.5)
        if check and result.returncode:
            raise AdbError(f"ADB failed ({result.returncode}): {' '.join(command)}\n{result.stderr.strip()}")
        return result

    def _reconnect_after_timeout(self) -> None:
        # A reconnect alone cannot recover a transport blocked by an orphaned
        # adb shell process. Restart the host server and establish a new
        # transport before retrying the original command.
        for host_args in (
            ("kill-server",),
            ("start-server",),
            ("connect", self.serial),
        ):
            try:
                subprocess.run(
                    [str(self.executable), *host_args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    check=False,
                    creationflags=CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired:
                continue
        time.sleep(1)

    @staticmethod
    def _is_transient_connection_error(output: str) -> bool:
        return any(
            marker in output
            for marker in (
                "error: closed",
                "device offline",
                "device not found",
                "no devices/emulators found",
                "cannot connect",
                "failed to read response",
                "connection reset",
                "protocol fault",
            )
        )

    def connect(self) -> None:
        subprocess.run(
            [str(self.executable), "connect", self.serial],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        state = self.run("get-state").stdout.strip()
        if state != "device":
            raise AdbError(f"MuMu device {self.serial} is not ready: {state}")

    def shell(self, *args: str, timeout: float = 20) -> str:
        return self.run("shell", *args, timeout=timeout).stdout

    def launch(
        self,
        package: str,
        *,
        component: str | None = None,
        reset_task: bool = False,
    ) -> None:
        if reset_task:
            try:
                self.shell("am", "force-stop", package, timeout=10)
            except AdbError as error:
                if "timed out" not in str(error).lower():
                    raise
        try:
            if component:
                self.shell("am", "start", "-n", component, timeout=10)
            else:
                self.shell(
                    "monkey",
                    "-p",
                    package,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                    timeout=12,
                )
        except AdbError as error:
            # The launch may have reached Android before the host command
            # timed out. Let the following UI dump decide whether it worked.
            if "timed out" not in str(error).lower():
                raise

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def tap_bounds(self, bounds: Bounds) -> None:
        self.tap(*bounds.center)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 450) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def back(self) -> None:
        self.shell("input", "keyevent", "4")

    def clear_focused_text(self, count: int = 50) -> None:
        self.shell("input", "keyevent", "123")
        self.shell("input", "keyevent", *(["67"] * count))

    def type_ascii(self, value: str) -> None:
        self.shell("input", "text", value)

    def dump_ui(self, name: str = "window", delay: float = 0.0) -> UiTree:
        if delay:
            time.sleep(delay)
        # MuMu's Android 15 UIAutomator can crash while replacing some named dumps.
        # A stable fixed remote path avoids that emulator-specific failure.
        remote = "/sdcard/window.xml"
        local = self.runtime_dir / f"{name}.xml"
        last_error: AdbError | None = None
        for attempt in range(6):
            try:
                self.run("shell", "rm", "-f", remote, timeout=5, check=False)
                dump_args = ["shell", "uiautomator", "dump"]
                if attempt % 2 == 0:
                    dump_args.append("--compressed")
                dump_args.append(remote)
                result = self.run(
                    *dump_args,
                    timeout=30, check=False,
                )
                output = f"{result.stdout}\n{result.stderr}"
                if result.returncode != 0 and "dumped to:" not in output:
                    raise AdbError(
                        f"UIAutomator dump failed ({result.returncode}): {result.stderr.strip()}"
                    )
                self.run("pull", remote, str(local), timeout=20)
                tree = UiTree.from_file(local)
                reason = tree.risk_reason()
                if reason:
                    raise RiskDetected(f"Page risk control detected: {reason}")
                return tree
            except (AdbError, ET.ParseError, OSError) as error:
                last_error = error if isinstance(error, AdbError) else AdbError(str(error))
                if self._needs_fast_dump(last_error):
                    # Continuous page animation can keep Android 15 from ever
                    # reporting an idle accessibility tree.
                    for setting in (
                        "window_animation_scale",
                        "transition_animation_scale",
                        "animator_duration_scale",
                    ):
                        self.run(
                            "shell", "settings", "put", "global", setting, "0",
                            timeout=5, check=False,
                        )
                    try:
                        return self._dump_ui_without_idle_wait(local)
                    except (AdbError, ET.ParseError, OSError) as fallback_error:
                        fallback = (
                            fallback_error
                            if isinstance(fallback_error, AdbError)
                            else AdbError(str(fallback_error))
                        )
                        raise AdbError(
                            f"{last_error}; fast UI fallback failed: {fallback}"
                        ) from fallback_error
                if attempt < 5:
                    time.sleep(min(1.5 * (attempt + 1), 4.5))
        raise last_error or AdbError("UIAutomator dump failed without an error message")

    @staticmethod
    def _needs_fast_dump(error: AdbError) -> bool:
        message = str(error).lower()
        return (
            "idle state" in message
            or "uiautomator dump failed (137)" in message
            or "uiautomator dump failed (139)" in message
        )

    def _fast_dump_jar(self) -> Path:
        candidates = (
            self.executable.parent / "price-monitor-fast-dump.jar",
            self.runtime_dir.parent / "tools" / "fast-ui-dump" / "fast-ui-dump.jar",
            Path.cwd() / "tools" / "fast-ui-dump" / "fast-ui-dump.jar",
        )
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 100:
                return candidate
        raise AdbError("Fast UI dump helper is missing from the installation")

    def _dump_ui_without_idle_wait(self, local: Path) -> UiTree:
        helper = self._fast_dump_jar()
        if not self._fast_dump_pushed:
            self.run("push", str(helper), self.FAST_DUMP_REMOTE_JAR, timeout=20)
            self._fast_dump_pushed = True

        last_error: AdbError | None = None
        for attempt in range(5):
            self.run(
                "shell", "rm", "-f", self.FAST_DUMP_REMOTE_XML,
                timeout=5, check=False,
            )
            result = self.run(
                "shell", "uiautomator", "runtest", self.FAST_DUMP_REMOTE_JAR,
                "-c", "com.pricemonitor.FastDump",
                timeout=15, check=False,
            )
            output = f"{result.stdout}\n{result.stderr}"
            if result.returncode == 0 and "failures!!!" not in output.lower():
                try:
                    self.run("pull", self.FAST_DUMP_REMOTE_XML, str(local), timeout=20)
                    tree = UiTree.from_file(local)
                    reason = tree.risk_reason()
                    if reason:
                        raise RiskDetected(f"Page risk control detected: {reason}")
                    return tree
                except (AdbError, ET.ParseError, OSError) as error:
                    last_error = error if isinstance(error, AdbError) else AdbError(str(error))
            else:
                last_error = AdbError(
                    f"Fast UI dump runner failed ({result.returncode}): {output.strip()}"
                )
            if attempt == 1:
                # MuMu can briefly finish the instrumentation command without
                # creating its XML after the stock dumper crashes. Re-pushing
                # also recovers if the emulator cleared /data/local/tmp.
                self.run(
                    "push", str(helper), self.FAST_DUMP_REMOTE_JAR,
                    timeout=20, check=False,
                )
                self._fast_dump_pushed = True
            if attempt < 4:
                time.sleep(min(1.25 * (attempt + 1), 4.0))
        raise last_error or AdbError("Fast UI dump runner produced no hierarchy")
