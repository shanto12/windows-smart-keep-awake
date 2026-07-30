#!/usr/bin/env python3
"""Minimal, idle-aware Windows keep-awake utility.

The program stays inside a configured local-time window (08:00-18:00 by
default), prevents system sleep while that window is active, and sends a
rare, non-text F24 key pulse only when the detected/configured inactivity
timeout is close.  It is intentionally Windows-only and dependency-free.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


LOGGER = logging.getLogger("keep_awake")

DEFAULT_HOURS = "08:00-18:00"
DEFAULT_LOCK_AFTER_SECONDS = 600.0
DEFAULT_WAKE_BEFORE_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 15.0
DEFAULT_COOLDOWN_SECONDS = 180.0
DEFAULT_INPUT_MODE = "f24"

KEYEVENTF_KEYUP = 0x0002
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
VK_F24 = 0x87

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

SPI_GETSCREENSAVETIMEOUT = 14
SPI_GETSCREENSAVEACTIVE = 16
SPI_GETSCREENSAVESECURE = 0x0076


def parse_clock(value: str) -> dt.time:
    """Parse a local time in HH:MM form."""

    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, ValueError):
        raise ValueError("time must use HH:MM, for example 08:00")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("time must be between 00:00 and 23:59")
    return dt.time(hour=hour, minute=minute)


def parse_hours(value: str) -> "Schedule":
    """Parse a schedule such as 08:00-18:00 or 22:00-06:00."""

    try:
        start_text, end_text = value.split("-", 1)
    except ValueError:
        raise ValueError("hours must use START-END, for example 08:00-18:00")
    return Schedule(parse_clock(start_text), parse_clock(end_text))


def parse_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number")
    if seconds <= 0:
        raise ValueError("seconds must be greater than zero")
    return seconds


def parse_nonnegative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number")
    if seconds < 0:
        raise ValueError("seconds cannot be negative")
    return seconds


def parse_lock_after(value: str) -> Optional[float]:
    if value.strip().lower() == "auto":
        return None
    return parse_seconds(value)


@dataclass(frozen=True)
class Schedule:
    """A daily local-time window; overnight windows are supported."""

    start: dt.time
    end: dt.time

    def contains(self, now: dt.datetime) -> bool:
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= now.time() < self.end
        return now.time() >= self.start or now.time() < self.end

    def seconds_until_start(self, now: dt.datetime) -> float:
        if self.contains(now):
            return 0.0
        start_date = now.date()
        if now.time() >= self.start:
            start_date += dt.timedelta(days=1)
        start = dt.datetime.combine(start_date, self.start)
        return max(0.0, (start - now).total_seconds())


@dataclass(frozen=True)
class Settings:
    schedule: Schedule
    lock_after_seconds: float
    wake_before_seconds: float = DEFAULT_WAKE_BEFORE_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    input_mode: str = DEFAULT_INPUT_MODE
    prevent_sleep: bool = True
    dry_run: bool = False


def should_pulse(
    scheduled: bool,
    idle_seconds: float,
    lock_after_seconds: float,
    wake_before_seconds: float,
    monotonic_now: float,
    last_pulse_monotonic: Optional[float],
    cooldown_seconds: float,
) -> bool:
    """Return True only when the idle timeout is close and cooldown allows it."""

    if not scheduled:
        return False
    threshold = max(0.0, lock_after_seconds - wake_before_seconds)
    if idle_seconds < threshold:
        return False
    if last_pulse_monotonic is not None:
        if monotonic_now - last_pulse_monotonic < cooldown_seconds:
            return False
    return True


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUT_UNION)]


class WindowsPlatform:
    """Small wrapper around the Windows APIs used by the runner."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("keep_awake.py must be run on Windows")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
        self.user32.GetLastInputInfo.restype = ctypes.c_bool
        self.kernel32.GetTickCount64.argtypes = []
        self.kernel32.GetTickCount64.restype = ctypes.c_uint64

        self.kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        self.kernel32.SetThreadExecutionState.restype = ctypes.c_uint32

        self.user32.SendInput.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        ]
        self.user32.SendInput.restype = ctypes.c_uint32
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        self.user32.GetCursorPos.restype = ctypes.c_bool

        self.user32.SystemParametersInfoW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.user32.SystemParametersInfoW.restype = ctypes.c_bool

    @staticmethod
    def _raise_last_error(operation: str) -> None:
        error = ctypes.get_last_error()
        if error:
            raise OSError(error, "%s failed" % operation)
        raise OSError("%s failed" % operation)

    def get_idle_seconds(self) -> float:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            self._raise_last_error("GetLastInputInfo")
        current_tick = int(self.kernel32.GetTickCount64())
        elapsed_ms = (current_tick - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed_ms / 1000.0

    def _system_parameter_bool(self, action: int) -> Optional[bool]:
        value = ctypes.c_int(0)
        if self.user32.SystemParametersInfoW(
            action, 0, ctypes.byref(value), 0
        ):
            return bool(value.value)
        return None

    def detect_lock_timeout(self) -> Tuple[float, str]:
        """Find the most conservative visible inactivity timeout.

        Windows policy can be managed centrally or hidden from a normal user.
        If no visible timeout is available, the documented fallback is ten
        minutes; users can always override it with --lock-after SECONDS.
        """

        candidates = []
        screen_saver_timeout = ctypes.c_uint32(0)
        screen_saver_active = self._system_parameter_bool(SPI_GETSCREENSAVEACTIVE)
        screen_saver_secure = self._system_parameter_bool(SPI_GETSCREENSAVESECURE)
        if self.user32.SystemParametersInfoW(
            SPI_GETSCREENSAVETIMEOUT,
            0,
            ctypes.byref(screen_saver_timeout),
            0,
        ):
            secure_from_registry = False
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop"
                ) as key:
                    secure_from_registry = str(
                        winreg.QueryValueEx(key, "ScreenSaveIsSecure")[0]
                    ) == "1"
            except (OSError, ImportError):
                pass
            if (
                screen_saver_timeout.value > 0
                and screen_saver_active
                and (screen_saver_secure or secure_from_registry)
            ):
                candidates.append(
                    (float(screen_saver_timeout.value), "Windows screen saver")
                )

        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
            ) as key:
                policy_seconds = int(
                    winreg.QueryValueEx(key, "InactivityTimeoutSecs")[0]
                )
                if policy_seconds > 0:
                    candidates.append(
                        (float(policy_seconds), "Windows inactivity policy")
                    )
        except (OSError, ImportError, TypeError, ValueError):
            pass

        if candidates:
            timeout, source = min(candidates, key=lambda candidate: candidate[0])
            return timeout, source
        return DEFAULT_LOCK_AFTER_SECONDS, "fallback (10 minutes)"

    def set_keep_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS
        if enabled:
            flags |= ES_SYSTEM_REQUIRED
        if not self.kernel32.SetThreadExecutionState(flags):
            self._raise_last_error("SetThreadExecutionState")

    def _send_inputs(self, inputs) -> None:
        input_array = (_INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(
            len(input_array), input_array, ctypes.sizeof(_INPUT)
        )
        if sent != len(input_array):
            self._raise_last_error("SendInput")

    def pulse_f24(self) -> None:
        key_down = _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(wVk=VK_F24, wScan=0, dwFlags=0, time=0, dwExtraInfo=0),
        )
        key_up = _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=VK_F24,
                wScan=0,
                dwFlags=KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=0,
            ),
        )
        self._send_inputs([key_down, key_up])

    def pulse_mouse(self) -> None:
        point = _POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            self._raise_last_error("GetCursorPos")
        direction = 1 if point.x == 0 else -1
        first = _INPUT(
            type=INPUT_MOUSE,
            mi=_MOUSEINPUT(
                dx=direction,
                dy=0,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE,
                time=0,
                dwExtraInfo=0,
            ),
        )
        second = _INPUT(
            type=INPUT_MOUSE,
            mi=_MOUSEINPUT(
                dx=-direction,
                dy=0,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE,
                time=0,
                dwExtraInfo=0,
            ),
        )
        self._send_inputs([first])
        time.sleep(0.02)
        self._send_inputs([second])

    def pulse(self, mode: str) -> None:
        if mode == "f24":
            self.pulse_f24()
        elif mode == "mouse":
            self.pulse_mouse()
        elif mode != "none":
            raise ValueError("unknown input mode: %s" % mode)


class KeepAwakeRunner:
    """Run the schedule with injectable clocks so decisions are testable."""

    def __init__(
        self,
        settings: Settings,
        platform: WindowsPlatform,
        now: Optional[Callable[[], dt.datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.settings = settings
        self.platform = platform
        self.now = now or dt.datetime.now
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.last_pulse_monotonic = None  # type: Optional[float]
        self.awake_enabled = False
        self.last_scheduled = None  # type: Optional[bool]

    def step(self) -> Tuple[bool, Optional[float]]:
        current = self.now()
        scheduled = self.settings.schedule.contains(current)
        if scheduled != self.last_scheduled:
            LOGGER.info(
                "%s schedule window at %s",
                "inside" if scheduled else "outside",
                current.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self.last_scheduled = scheduled

        if not scheduled:
            if self.awake_enabled:
                self.platform.set_keep_awake(False)
                self.awake_enabled = False
            return False, None

        if self.settings.prevent_sleep and not self.settings.dry_run:
            if not self.awake_enabled:
                self.platform.set_keep_awake(True)
                self.awake_enabled = True

        idle_seconds = self.platform.get_idle_seconds()
        now_monotonic = self.monotonic()
        if should_pulse(
            scheduled=True,
            idle_seconds=idle_seconds,
            lock_after_seconds=self.settings.lock_after_seconds,
            wake_before_seconds=self.settings.wake_before_seconds,
            monotonic_now=now_monotonic,
            last_pulse_monotonic=self.last_pulse_monotonic,
            cooldown_seconds=self.settings.cooldown_seconds,
        ):
            if self.settings.input_mode != "none" and not self.settings.dry_run:
                self.platform.pulse(self.settings.input_mode)
            self.last_pulse_monotonic = now_monotonic
            LOGGER.info(
                "%s idle pulse at %.1fs idle (mode=%s)",
                "would send" if self.settings.dry_run else "sent",
                idle_seconds,
                self.settings.input_mode,
            )
        return True, idle_seconds

    def run(self, once: bool = False) -> None:
        try:
            while True:
                scheduled, idle_seconds = self.step()
                if once:
                    return

                current = self.now()
                if not scheduled:
                    delay = min(
                        3600.0,
                        max(0.5, self.settings.schedule.seconds_until_start(current)),
                    )
                else:
                    delay = self.settings.poll_seconds
                    if idle_seconds is not None:
                        until_threshold = (
                            self.settings.lock_after_seconds
                            - self.settings.wake_before_seconds
                            - idle_seconds
                        )
                        if until_threshold > 0:
                            delay = min(delay, max(0.5, until_threshold))
                self.sleep(delay)
        finally:
            if self.awake_enabled:
                self.platform.set_keep_awake(False)
                self.awake_enabled = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Keep a Windows PC awake during a local-time window with "
            "minimal, idle-aware input."
        )
    )
    parser.add_argument(
        "--hours",
        default=DEFAULT_HOURS,
        metavar="START-END",
        help="local daily window, default: %(default)s",
    )
    parser.add_argument(
        "--lock-after",
        default="auto",
        metavar="SECONDS|auto",
        help="inactivity timeout; auto-detect or override it explicitly",
    )
    parser.add_argument(
        "--wake-before",
        type=parse_nonnegative_seconds,
        default=DEFAULT_WAKE_BEFORE_SECONDS,
        metavar="SECONDS",
        help="pulse this many seconds before the timeout, default: %(default)s",
    )
    parser.add_argument(
        "--poll",
        type=parse_seconds,
        default=DEFAULT_POLL_SECONDS,
        metavar="SECONDS",
        help="maximum idle check interval, default: %(default)s",
    )
    parser.add_argument(
        "--cooldown",
        type=parse_nonnegative_seconds,
        default=DEFAULT_COOLDOWN_SECONDS,
        metavar="SECONDS",
        help="minimum time between pulses, default: %(default)s",
    )
    parser.add_argument(
        "--input",
        choices=("f24", "mouse", "none"),
        default=DEFAULT_INPUT_MODE,
        help="input pulse: rare F24 key, one-pixel mouse nudge, or none",
    )
    parser.add_argument(
        "--no-sleep-prevention",
        action="store_true",
        help="do not call SetThreadExecutionState during the active window",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log decisions without changing power state or sending input",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="evaluate one iteration and exit",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable informational logging",
    )
    parser.add_argument("--version", action="version", version="1.0.0")
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        schedule = parse_hours(args.hours)
        lock_after = parse_lock_after(args.lock_after)
    except ValueError as error:
        parser.error(str(error))

    if os.name != "nt":
        parser.error("this utility must be run on Windows")

    platform = WindowsPlatform()
    if lock_after is None:
        lock_after, source = platform.detect_lock_timeout()
        LOGGER.info("using %.0fs timeout from %s", lock_after, source)
    else:
        source = "command-line override"
        LOGGER.info("using %.0fs timeout from %s", lock_after, source)

    settings = Settings(
        schedule=schedule,
        lock_after_seconds=lock_after,
        wake_before_seconds=args.wake_before,
        poll_seconds=args.poll,
        cooldown_seconds=args.cooldown,
        input_mode=args.input,
        prevent_sleep=not args.no_sleep_prevention,
        dry_run=args.dry_run,
    )
    LOGGER.info(
        "active from %s to %s; input=%s; sleep-prevention=%s; dry-run=%s",
        schedule.start.strftime("%H:%M"),
        schedule.end.strftime("%H:%M"),
        settings.input_mode,
        settings.prevent_sleep and not settings.dry_run,
        settings.dry_run,
    )

    runner = KeepAwakeRunner(settings, platform)
    try:
        runner.run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.info("stopped")
    except OSError as error:
        LOGGER.error("Windows API error: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
