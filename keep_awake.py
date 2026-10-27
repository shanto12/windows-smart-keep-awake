#!/usr/bin/env python3
"""Windows Smart Keep Awake.

Keeps a Windows laptop or VM awake during work hours, keeps Microsoft Teams
"Available" while you are away from the keyboard, and lets Teams go "Away"
naturally during a lunch break. Python 3.9+, standard library only, one file.

Two different things are handled, because Windows treats them differently:

1. Machine awake (sleep / display off). Uses SetThreadExecutionState, the
   documented API that video players and PowerToys Awake use. No input is
   simulated, nothing is written to disk or registry, and the request ends
   automatically when this process exits.

2. Teams presence and screen lock. Teams marks you Away after about five
   minutes without keyboard/mouse input (the same idle timer that drives the
   screen saver and inactivity lock). A power request does NOT reset that
   timer, so during work hours, and only after you have been idle for a
   while, one F24 key tap is sent. F24 exists on no physical keyboard, types
   nothing and moves nothing. If Windows does not register it, the script
   switches to a 1-pixel mouse move-and-return.

During breaks (default 12:00-13:00) no input is sent, so Teams turns Away on
its own after its normal timeout while the machine itself stays awake.
Outside work hours everything is released and Windows behaves normally.

The script never runs while you are typing, never touches a locked or secure
desktop, never unlocks anything, and changes no settings. Run it only on a
device and in a way your organisation permits.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, FrozenSet, Optional, Sequence, Tuple

VERSION = "4.0.0"
LOG = logging.getLogger("keep_awake")

WORK, BREAK, OFF = "work", "break", "off"
DEFAULT_HOURS = "08:00-18:00"
DEFAULT_BREAKS = ("12:00-13:00",)
DEFAULT_DAYS = "mon-fri"
DEFAULT_IDLE_SECONDS = 120.0  # Teams goes Away after ~300s; stay well inside it.
MIN_IDLE_SECONDS = 10.0
VERIFY_SECONDS = 2.0
RETRY_SECONDS = 300.0
OFF_POLL_SECONDS = 30.0
HEARTBEAT_SECONDS = 1800.0
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

TimeRange = Tuple[dt.time, dt.time]


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------


def parse_clock(text: str) -> dt.time:
    try:
        hour, minute = (int(part) for part in text.strip().split(":"))
    except ValueError:
        raise ValueError("time %r must use HH:MM" % text) from None
    if hour == 24 and minute == 0:
        return dt.time(0, 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time %r is out of range" % text)
    return dt.time(hour, minute)


def parse_range(text: str) -> TimeRange:
    start, sep, end = text.partition("-")
    if not sep:
        raise ValueError("range %r must use HH:MM-HH:MM" % text)
    return parse_clock(start), parse_clock(end)


def parse_days(text: str) -> FrozenSet[int]:
    days = set()
    for part in text.lower().replace(" ", "").split(","):
        first, sep, last = part.partition("-")
        if first not in DAY_NAMES or (sep and last not in DAY_NAMES):
            raise ValueError("days must look like mon-fri or mon,wed,fri")
        a = DAY_NAMES.index(first)
        b = DAY_NAMES.index(last) if sep else a
        days.update(range(a, b + 1) if a <= b else (*range(a, 7), *range(0, b + 1)))
    return frozenset(days)


def in_range(moment: dt.time, span: TimeRange) -> bool:
    start, end = span
    if start == end:
        return True
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end  # overnight span


def format_range(span: TimeRange) -> str:
    return "%s-%s" % (span[0].strftime("%H:%M"), span[1].strftime("%H:%M"))


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


@lru_cache(maxsize=8)
def us_federal_holidays(year: int) -> Dict[dt.date, str]:
    """US federal holidays for ``year``, including weekend observance."""
    result: Dict[dt.date, str] = {}
    for y in (year - 1, year, year + 1):  # Jan 1 can be observed on Dec 31
        may_31 = dt.date(y, 5, 31)
        dates = {
            dt.date(y, 1, 1): "New Year's Day",
            _nth_weekday(y, 1, 0, 3): "Martin Luther King Jr. Day",
            _nth_weekday(y, 2, 0, 3): "Washington's Birthday",
            may_31 - dt.timedelta(days=may_31.weekday()): "Memorial Day",
            dt.date(y, 6, 19): "Juneteenth",
            dt.date(y, 7, 4): "Independence Day",
            _nth_weekday(y, 9, 0, 1): "Labor Day",
            _nth_weekday(y, 10, 0, 2): "Columbus Day",
            dt.date(y, 11, 11): "Veterans Day",
            _nth_weekday(y, 11, 3, 4): "Thanksgiving Day",
            dt.date(y, 12, 25): "Christmas Day",
        }
        for day, name in dates.items():
            shift = {5: -1, 6: 1}.get(day.weekday(), 0)
            observed = day + dt.timedelta(days=shift)
            if observed.year == year:
                result[observed] = name + (" (observed)" if shift else "")
    return result


@dataclass(frozen=True)
class Schedule:
    hours: TimeRange = (dt.time(8), dt.time(18))
    breaks: Tuple[TimeRange, ...] = ((dt.time(12), dt.time(13)),)
    days: FrozenSet[int] = frozenset(range(5))
    holidays: FrozenSet[dt.date] = frozenset()
    us_holidays: bool = False

    def phase(self, now: dt.datetime) -> Tuple[str, str]:
        """Return (WORK|BREAK|OFF, human-readable reason) for a local time."""
        day = now.date()
        if day in self.holidays:
            return OFF, "holiday"
        if self.us_holidays and day in us_federal_holidays(day.year):
            return OFF, us_federal_holidays(day.year)[day]
        if now.weekday() not in self.days:
            return OFF, "not a work day"
        moment = now.time()
        if not in_range(moment, self.hours):
            return OFF, "outside %s" % format_range(self.hours)
        for span in self.breaks:
            if in_range(moment, span):
                return BREAK, "break %s" % format_range(span)
        return WORK, "work hours %s" % format_range(self.hours)


def effective_idle_seconds(requested: float, lock_limits: Sequence[Optional[float]]) -> float:
    """Pulse at the requested idle time, or at half of a shorter lock timeout."""
    limits = [v for v in lock_limits if v and v > 0 and math.isfinite(v)]
    chosen = min([requested] + [v / 2 for v in limits])
    return max(MIN_IDLE_SECONDS, chosen)


# --------------------------------------------------------------------------
# Windows platform (ctypes only)
# --------------------------------------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
VK_F24 = 0x87
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
SPI_GETSCREENSAVEACTIVE = 0x0010
SPI_GETSCREENSAVETIMEOUT = 0x000E
DESKTOP_READOBJECTS = 0x0001
UOI_NAME, UOI_IO = 2, 6
ERROR_ALREADY_EXISTS = 183
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
# Shift, Ctrl, Alt, Win (L/R), F24, mouse buttons: never inject while held.
HELD_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C, VK_F24, 0x01, 0x02, 0x04, 0x05, 0x06)

# Windows LONG/DWORD are 32-bit even on 64-bit Python.
DWORD, LONG, WORD, BOOL = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint16, ctypes.c_int32
ULONG_PTR = ctypes.c_size_t


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("dwTime", DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


class WindowsPlatform:
    """Thin wrapper around the handful of documented Win32 calls we need."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("this needs Windows; use --dry-run to preview elsewhere")
        k32 = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        for fn, args, res in (
            (k32.SetThreadExecutionState, [DWORD], DWORD),
            (k32.GetTickCount, [], DWORD),
            (k32.CreateMutexW, [ctypes.c_void_p, BOOL, ctypes.c_wchar_p], ctypes.c_void_p),
            (u32.GetLastInputInfo, [ctypes.POINTER(LASTINPUTINFO)], BOOL),
            (u32.SendInput, [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int], ctypes.c_uint),
            (u32.SystemParametersInfoW, [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                         ctypes.c_uint], BOOL),
            (u32.OpenInputDesktop, [DWORD, BOOL, DWORD], ctypes.c_void_p),
            (u32.GetUserObjectInformationW, [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                             DWORD, ctypes.POINTER(DWORD)], BOOL),
            (u32.CloseDesktop, [ctypes.c_void_p], BOOL),
            (u32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_int16),
            (u32.GetCursorPos, [ctypes.POINTER(POINT)], BOOL),
            (u32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int),
        ):
            fn.argtypes, fn.restype = args, res
        self._mutex = None

    # -- process ----------------------------------------------------------
    def claim_single_instance(self) -> bool:
        self._mutex = self.kernel32.CreateMutexW(None, False, "Local\\WindowsSmartKeepAwake")
        return not (self._mutex and ctypes.get_last_error() == ERROR_ALREADY_EXISTS)

    # -- power ------------------------------------------------------------
    def set_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if enabled else 0)
        if not self.kernel32.SetThreadExecutionState(flags):
            raise OSError("Windows rejected the keep-awake power request")

    # -- idle state -------------------------------------------------------
    def input_state(self) -> Tuple[int, float]:
        """Return (last-input tick, seconds since last input)."""
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError("GetLastInputInfo failed")
        elapsed = (self.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return info.dwTime, elapsed / 1000.0

    def lock_timeouts(self) -> Tuple[Optional[float], Optional[float]]:
        """(screen-saver timeout, machine inactivity lock) in seconds, if set."""
        def spi(action: int) -> Optional[int]:
            value = DWORD()
            ok = self.user32.SystemParametersInfoW(action, 0, ctypes.byref(value), 0)
            return value.value if ok else None

        saver = spi(SPI_GETSCREENSAVETIMEOUT) if spi(SPI_GETSCREENSAVEACTIVE) else None
        machine = None
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, kind = winreg.QueryValueEx(key, "InactivityTimeoutSecs")
                if kind == winreg.REG_DWORD and value > 0:
                    machine = float(value)
        except OSError:
            pass
        return (float(saver) if saver else None), machine

    def desktop_ready(self) -> bool:
        """True only on the normal, unlocked, interactive desktop."""
        desk = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desk:
            return False
        try:
            name = ctypes.create_unicode_buffer(64)
            receiving = BOOL()
            needed = DWORD()
            return bool(
                self.user32.GetUserObjectInformationW(desk, UOI_NAME, name, ctypes.sizeof(name),
                                                      ctypes.byref(needed))
                and self.user32.GetUserObjectInformationW(desk, UOI_IO, ctypes.byref(receiving),
                                                          ctypes.sizeof(receiving),
                                                          ctypes.byref(needed))
                and name.value.lower() == "default"
                and receiving.value
            )
        finally:
            self.user32.CloseDesktop(desk)

    def user_holding_input(self) -> bool:
        return any(self.user32.GetAsyncKeyState(vk) & 0x8000 for vk in HELD_KEYS)

    # -- input ------------------------------------------------------------
    def _f24_events(self):
        events = (INPUT * 2)()
        for event in events:
            event.type = INPUT_KEYBOARD
            event.ki.wVk = VK_F24
        events[1].ki.dwFlags = KEYEVENTF_KEYUP
        return events

    def _mouse_events(self):
        point = POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            return None
        left, top, width, height = (self.user32.GetSystemMetrics(i) for i in (
            SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN))
        if width < 2 or height < 1:
            return None
        nudge_x = point.x + (1 if point.x < left + width - 1 else -1)
        events = (INPUT * 2)()
        # Absolute coordinates: immune to pointer acceleration, so it returns exactly.
        for event, x in zip(events, (nudge_x, point.x)):
            event.type = INPUT_MOUSE
            event.mi.dx = min(65535, int((x - left + 0.5) * 65536 / width))
            event.mi.dy = min(65535, int((point.y - top + 0.5) * 65536 / height))
            event.mi.dwFlags = (MOUSEEVENTF_MOVE | MOUSEEVENTF_MOVE_NOCOALESCE
                                | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_ABSOLUTE)
        return events

    def send_activity(self, mode: str) -> bool:
        events = self._f24_events() if mode == "f24" else self._mouse_events()
        if events is None:
            return False
        sent = self.user32.SendInput(2, events, ctypes.sizeof(INPUT))
        if sent == 1:  # never leave F24 down or the pointer moved
            self.user32.SendInput(1, (INPUT * 1)(events[1]), ctypes.sizeof(INPUT))
        return sent == 2


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class Runner:
    def __init__(
        self,
        platform,
        schedule: Schedule,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        input_mode: str = "auto",
        awake_on_breaks: bool = True,
        awake_off_hours: bool = False,
        duration: Optional[float] = None,
        now: Callable[[], dt.datetime] = dt.datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.platform = platform
        self.schedule = schedule
        self.requested_idle = idle_seconds
        self.input_mode = input_mode
        self.mode = "mouse" if input_mode == "mouse" else "f24"
        self.awake_on_breaks = awake_on_breaks
        self.awake_off_hours = awake_off_hours
        self.duration = duration
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.heartbeat_seconds = heartbeat_seconds
        self.threshold = idle_seconds
        self.awake = False
        self.pulses = 0
        self._pending: Optional[Tuple[int, float]] = None  # (tick before pulse, verify by)
        self._retry_at = 0.0
        self._paused: Optional[str] = None

    # -- helpers ----------------------------------------------------------
    def _set_awake(self, enabled: bool) -> None:
        if enabled != self.awake:
            self.platform.set_awake(enabled)
            self.awake = enabled
            LOG.info("Keep-awake %s", "ON (no sleep, display stays on)" if enabled else
                     "OFF (normal Windows power settings)")

    def _pause(self, reason: Optional[str]) -> None:
        if reason != self._paused:
            if reason:
                LOG.info("Activity paused: %s", reason)
            elif self._paused:
                LOG.info("Activity resumed")
            self._paused = reason

    def _input_failed(self, why: str, clock: float) -> None:
        if self.input_mode == "auto" and self.mode == "f24":
            LOG.warning("F24 %s; switching to 1-pixel mouse nudge", why)
            self.mode = "mouse"
            return
        LOG.warning("%s input %s; retrying in %d min (keep-awake unaffected)",
                    self.mode, why, RETRY_SECONDS // 60)
        self._retry_at = clock + RETRY_SECONDS
        if self.input_mode == "auto":
            self.mode = "f24"  # start the auto sequence again next time

    def _guard_activity(self, clock: float) -> None:
        """Keep the idle timer below the threshold; only ever when user is idle."""
        if self._pending is not None:
            tick_before, verify_by = self._pending
            tick, idle = self.platform.input_state()
            if tick != tick_before:
                self._pending = None
                self.pulses += 1
                LOG.debug("%s registered by Windows (idle reset)", self.mode)
            elif clock >= verify_by:
                self._pending = None
                self._input_failed("was not registered by Windows", clock)
            return
        if clock < self._retry_at:
            return
        if not self.platform.desktop_ready():
            self._pause("screen locked or session not interactive")
            return
        self._pause(None)
        tick, idle = self.platform.input_state()
        if idle < self.threshold or self.platform.user_holding_input():
            return
        if self.platform.send_activity(self.mode):
            self._pending = (tick, clock + VERIFY_SECONDS)
        else:
            self._input_failed("was rejected by SendInput", clock)

    # -- main loop --------------------------------------------------------
    def run(self) -> int:
        self.threshold = effective_idle_seconds(self.requested_idle, self.platform.lock_timeouts())
        LOG.info("Windows Smart Keep Awake v%s | days %s | hours %s | breaks %s | "
                 "activity after %.0fs idle | Ctrl+C to stop",
                 VERSION, ",".join(DAY_NAMES[d] for d in sorted(self.schedule.days)),
                 format_range(self.schedule.hours),
                 ", ".join(format_range(b) for b in self.schedule.breaks) or "none",
                 self.threshold)
        start = self.monotonic()
        next_beat = start + self.heartbeat_seconds
        last_phase = None
        try:
            while True:
                clock = self.monotonic()
                if self.duration is not None and clock - start >= self.duration:
                    return 0
                phase, reason = self.schedule.phase(self.now())
                if (phase, reason) != last_phase:
                    LOG.info("%s: %s", {WORK: "WORK", BREAK: "BREAK", OFF: "OFF"}[phase], {
                        WORK: "keeping PC awake and Teams Available",
                        BREAK: "no activity sent, Teams will go Away; PC %s" % (
                            "stays awake" if self.awake_on_breaks or self.awake_off_hours
                            else "may sleep"),
                        OFF: "idle (%s)" % reason,
                    }[phase] + ("" if phase == OFF else " (%s)" % reason))
                    last_phase = (phase, reason)
                    self._pending = None
                    self._paused = None
                self._set_awake(phase == WORK or self.awake_off_hours
                                or (phase == BREAK and self.awake_on_breaks))
                if phase == WORK and self.input_mode != "none":
                    self._guard_activity(clock)
                if clock >= next_beat:
                    LOG.info("Still running: %s, activity pulses so far %d", phase, self.pulses)
                    next_beat = clock + self.heartbeat_seconds
                if phase == WORK and self.input_mode != "none":
                    delay = 0.5 if self._pending else min(5.0, self.threshold / 4)
                else:
                    delay = OFF_POLL_SECONDS
                if self.duration is not None:
                    delay = min(delay, max(0.01, start + self.duration - self.monotonic()))
                self.sleep(delay)
        finally:
            if self.awake:
                self._set_awake(False)


# --------------------------------------------------------------------------
# Dry-run platform and CLI
# --------------------------------------------------------------------------


class DryRunPlatform:
    """Logs what would happen; never touches Windows. Works on any OS."""

    def __init__(self) -> None:
        self.tick = 0

    def claim_single_instance(self) -> bool:
        return True

    def set_awake(self, enabled: bool) -> None:
        LOG.debug("[dry-run] SetThreadExecutionState(%s)", enabled)

    def lock_timeouts(self):
        return None, None

    def desktop_ready(self) -> bool:
        return True

    def user_holding_input(self) -> bool:
        return False

    def input_state(self) -> Tuple[int, float]:
        return self.tick, 10**6  # pretend the user is always idle

    def send_activity(self, mode: str) -> bool:
        LOG.info("[dry-run] would send %s activity", mode)
        self.tick += 1
        return True


def _arg(parser_fn):
    def wrapped(text):
        try:
            return parser_fn(text)
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from None
    return wrapped


def _seconds(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("must be a positive number of seconds")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="keep_awake.py",
        description="Keep Windows awake and Teams Available during work hours; "
                    "let Teams go Away during breaks.",
        allow_abbrev=False,
    )
    p.add_argument("--hours", type=_arg(parse_range), default=parse_range(DEFAULT_HOURS),
                   metavar="HH:MM-HH:MM", help="work hours (default %s)" % DEFAULT_HOURS)
    p.add_argument("--break", dest="breaks", type=_arg(parse_range), action="append",
                   metavar="HH:MM-HH:MM",
                   help="break when Teams may go Away; repeatable (default %s)"
                        % ", ".join(DEFAULT_BREAKS))
    p.add_argument("--no-breaks", action="store_true", help="no breaks at all")
    p.add_argument("--days", type=_arg(parse_days), default=parse_days(DEFAULT_DAYS),
                   metavar="DAYS", help="work days, e.g. mon-fri or mon,tue,thu (default mon-fri)")
    p.add_argument("--holiday", type=_arg(dt.date.fromisoformat), action="append", default=[],
                   metavar="YYYY-MM-DD", help="day off; repeatable")
    p.add_argument("--us-holidays", action="store_true",
                   help="also treat US federal holidays (observed dates) as days off")
    p.add_argument("--idle", type=_arg(_seconds), default=DEFAULT_IDLE_SECONDS, metavar="SECONDS",
                   help="send activity only after this much idle time (default %.0f; lowered "
                        "automatically for shorter lock timeouts)" % DEFAULT_IDLE_SECONDS)
    p.add_argument("--input", dest="input_mode", choices=("auto", "f24", "mouse", "none"),
                   default="auto",
                   help="auto = F24 key, falling back to a 1-pixel mouse nudge (default); "
                        "none = keep awake only, Teams may go Away")
    p.add_argument("--sleep-on-breaks", action="store_true",
                   help="also release the keep-awake request during breaks")
    p.add_argument("--always-awake", action="store_true",
                   help="keep the PC awake outside work hours too (no input is sent)")
    p.add_argument("--duration", type=_arg(_seconds), metavar="SECONDS",
                   help="stop after this many seconds")
    p.add_argument("--log-file", metavar="PATH", help="also append log lines to this file")
    p.add_argument("--dry-run", action="store_true",
                   help="simulate without Windows calls (any OS) and show the current phase")
    p.add_argument("--status", action="store_true",
                   help="print schedule phase and Windows idle/lock info, then exit")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def configure_logging(verbose: bool, log_file: Optional[str]) -> None:
    LOG.handlers.clear()
    LOG.propagate = False
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(fmt)
        LOG.addHandler(handler)


def status(platform, schedule: Schedule, idle_seconds: float) -> None:
    phase, reason = schedule.phase(dt.datetime.now())
    saver, machine = platform.lock_timeouts()
    _, idle = platform.input_state()
    print("Windows Smart Keep Awake v%s" % VERSION)
    print("  Now:                  %s (%s)" % (phase.upper(), reason))
    print("  Session idle:         %.0fs" % idle)
    print("  Desktop interactive:  %s" % platform.desktop_ready())
    print("  Screen-saver timeout: %s" % ("%.0fs" % saver if saver else "off"))
    print("  Policy lock timeout:  %s" % ("%.0fs" % machine if machine else "not set"))
    print("  Activity threshold:   %.0fs idle" % effective_idle_seconds(idle_seconds,
                                                                      (saver, machine)))


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    breaks = () if args.no_breaks else tuple(
        args.breaks or (parse_range(b) for b in DEFAULT_BREAKS))
    schedule = Schedule(args.hours, breaks, args.days, frozenset(args.holiday), args.us_holidays)
    try:
        platform = DryRunPlatform() if args.dry_run else WindowsPlatform()
        if args.status:
            status(platform, schedule, args.idle)
            return 0
        if not platform.claim_single_instance():
            LOG.error("Already running in another window; close that one first")
            return 1
        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), _interrupt)
        runner = Runner(
            platform, schedule,
            idle_seconds=args.idle,
            input_mode=args.input_mode,
            awake_on_breaks=not args.sleep_on_breaks,
            awake_off_hours=args.always_awake,
            duration=args.duration if not args.dry_run else (args.duration or 1.0),
        )
        return runner.run()
    except KeyboardInterrupt:
        LOG.info("Stopped")
        return 0
    except OSError as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
