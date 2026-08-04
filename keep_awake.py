#!/usr/bin/env python3
"""Minimal Windows keep-awake utility that also keeps MS Teams active.

Inside a configured local-time window (08:00-18:00 by default) the program
prevents system sleep, keeps the display on, and sends a rare, non-text F24
key pulse whenever the machine has been idle for --max-idle seconds.  The
pulse resets Windows' last-input timer -- the same timer Microsoft Teams
uses for presence -- so pulsing before Teams' five-minute away threshold
keeps the status "Available".  Console output is always on: a dot every
--heartbeat seconds shows the script is alive, a star marks each pulse.

It is intentionally Windows-only and dependency-free.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


DEFAULT_HOURS = "08:00-18:00"
DEFAULT_MAX_IDLE_SECONDS = 240.0  # pulse before Teams' 5-minute away timer
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_HEARTBEAT_SECONDS = 300.0
DEFAULT_INPUT_MODE = "f24"
VERSION = "2.0.0"

KEYEVENTF_KEYUP = 0x0002
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
VK_F24 = 0x87

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


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
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    input_mode: str = DEFAULT_INPUT_MODE
    dry_run: bool = False


def should_pulse(
    scheduled: bool,
    idle_seconds: float,
    max_idle_seconds: float,
    monotonic_now: float,
    last_pulse_monotonic: Optional[float],
) -> bool:
    """Pulse only inside the window, once idle reaches the limit.

    The elapsed-time guard keeps --input none and --dry-run from firing on
    every poll, since those modes never reset the system idle timer.
    """

    if not scheduled:
        return False
    if idle_seconds < max_idle_seconds:
        return False
    if last_pulse_monotonic is not None:
        if monotonic_now - last_pulse_monotonic < max_idle_seconds:
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

    def set_keep_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS
        if enabled:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
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


def _print_echo(text: str) -> None:
    print(text, end="", flush=True)


class KeepAwakeRunner:
    """Run the schedule with injectable clocks so decisions are testable."""

    def __init__(
        self,
        settings: Settings,
        platform: WindowsPlatform,
        now: Optional[Callable[[], dt.datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        echo: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings = settings
        self.platform = platform
        self.now = now or dt.datetime.now
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.echo = echo if echo is not None else _print_echo
        self.last_pulse_monotonic = None  # type: Optional[float]
        self.last_heartbeat_monotonic = None  # type: Optional[float]
        self.awake_enabled = False
        self.last_scheduled = None  # type: Optional[bool]
        self._mid_line = False

    def _status(self, message: str) -> None:
        prefix = "\n" if self._mid_line else ""
        stamp = self.now().strftime("%Y-%m-%d %H:%M:%S")
        self.echo("%s[%s] %s\n" % (prefix, stamp, message))
        self._mid_line = False

    def _tick(self, mark: str) -> None:
        self.echo(mark)
        self._mid_line = True

    def step(self) -> Tuple[bool, Optional[float]]:
        current = self.now()
        scheduled = self.settings.schedule.contains(current)
        if scheduled != self.last_scheduled:
            if scheduled:
                self._status(
                    "inside active window %s-%s: keeping Windows awake and "
                    "Teams available"
                    % (
                        self.settings.schedule.start.strftime("%H:%M"),
                        self.settings.schedule.end.strftime("%H:%M"),
                    )
                )
            else:
                self._status(
                    "outside active window: not intervening until %s"
                    % self.settings.schedule.start.strftime("%H:%M")
                )
            self.last_scheduled = scheduled

        if not scheduled:
            if self.awake_enabled:
                self.platform.set_keep_awake(False)
                self.awake_enabled = False
            return False, None

        if not self.settings.dry_run and not self.awake_enabled:
            self.platform.set_keep_awake(True)
            self.awake_enabled = True

        idle_seconds = self.platform.get_idle_seconds()
        now_monotonic = self.monotonic()
        if should_pulse(
            scheduled=True,
            idle_seconds=idle_seconds,
            max_idle_seconds=self.settings.max_idle_seconds,
            monotonic_now=now_monotonic,
            last_pulse_monotonic=self.last_pulse_monotonic,
        ):
            if self.settings.input_mode != "none" and not self.settings.dry_run:
                self.platform.pulse(self.settings.input_mode)
            self.last_pulse_monotonic = now_monotonic
            self._tick("*")
        return True, idle_seconds

    def run(self, once: bool = False) -> None:
        try:
            while True:
                scheduled, _ = self.step()
                if once:
                    return

                now_monotonic = self.monotonic()
                if self.last_heartbeat_monotonic is None:
                    self.last_heartbeat_monotonic = now_monotonic
                elif (
                    now_monotonic - self.last_heartbeat_monotonic
                    >= self.settings.heartbeat_seconds
                ):
                    self._tick(".")
                    self.last_heartbeat_monotonic = now_monotonic

                if scheduled:
                    delay = self.settings.poll_seconds
                else:
                    delay = min(
                        3600.0,
                        max(
                            0.5,
                            self.settings.schedule.seconds_until_start(self.now()),
                        ),
                    )
                until_heartbeat = self.settings.heartbeat_seconds - (
                    now_monotonic - self.last_heartbeat_monotonic
                )
                delay = min(delay, max(0.5, until_heartbeat))
                self.sleep(delay)
        finally:
            if self.awake_enabled:
                self.platform.set_keep_awake(False)
                self.awake_enabled = False
                self._status("released keep-awake state")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Keep a Windows PC awake and MS Teams available during a "
            "local-time window, using minimal idle-aware input."
        )
    )
    parser.add_argument(
        "--hours",
        default=DEFAULT_HOURS,
        metavar="START-END",
        help="local daily window, default: %(default)s",
    )
    parser.add_argument(
        "--max-idle",
        type=parse_seconds,
        default=DEFAULT_MAX_IDLE_SECONDS,
        metavar="SECONDS",
        help=(
            "pulse when idle reaches this many seconds; keep it under 300 "
            "so Teams never goes away, default: %(default)s"
        ),
    )
    parser.add_argument(
        "--poll",
        type=parse_seconds,
        default=DEFAULT_POLL_SECONDS,
        metavar="SECONDS",
        help="idle check interval, default: %(default)s",
    )
    parser.add_argument(
        "--heartbeat",
        type=parse_seconds,
        default=DEFAULT_HEARTBEAT_SECONDS,
        metavar="SECONDS",
        help="print a liveness dot this often, default: %(default)s",
    )
    parser.add_argument(
        "--input",
        choices=("f24", "mouse", "none"),
        default=DEFAULT_INPUT_MODE,
        help="input pulse: rare F24 key, one-pixel mouse nudge, or none",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print decisions without changing power state or sending input",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="evaluate one iteration and exit",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        schedule = parse_hours(args.hours)
    except ValueError as error:
        parser.error(str(error))

    if os.name != "nt":
        parser.error("this utility must be run on Windows")

    settings = Settings(
        schedule=schedule,
        max_idle_seconds=args.max_idle,
        poll_seconds=args.poll,
        heartbeat_seconds=args.heartbeat,
        input_mode=args.input,
        dry_run=args.dry_run,
    )

    print("windows-smart-keep-awake v%s" % VERSION, flush=True)
    print(
        "active window %s-%s local time; %s pulse when idle >= %.0fs"
        % (
            schedule.start.strftime("%H:%M"),
            schedule.end.strftime("%H:%M"),
            settings.input_mode,
            settings.max_idle_seconds,
        ),
        flush=True,
    )
    print(
        "output: '.' every %.0fs means still running, '*' means pulse sent"
        % settings.heartbeat_seconds,
        flush=True,
    )
    if settings.dry_run:
        print("dry-run: no power state change, no input sent", flush=True)
    print("press Ctrl+C to stop", flush=True)

    platform = WindowsPlatform()
    runner = KeepAwakeRunner(settings, platform)
    try:
        runner.run(once=args.once)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    except OSError as error:
        print("\nWindows API error: %s" % error, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
