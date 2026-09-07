#!/usr/bin/env python3
"""Keep Windows awake continuously, with work-hours Teams activity support.

Python 3.9+, standard library only. No Microsoft API authentication, app
registration, credentials, network calls, or additional packages. Teams must
already be signed in normally; this script does not operate its account.

RUN ON WINDOWS
    py keep_awake_teams.py
    py keep_awake_teams.py --duration 3600
    py keep_awake_teams.py --diagnose
    py keep_awake_teams.py --dry-run
    py keep_awake_teams.py --list-holidays 2027
    py keep_awake_teams.py --holiday 2026-12-31
    py keep_awake_teams.py --work-input f24
    py keep_awake_teams.py --docs

DEFAULT BEHAVIOR (VERSION 3.1)
    System/display wakefulness and inactivity-lock prevention continue for the
    entire run, including nights, weekends, and holidays. The script holds a
    thread-scoped system/display power request and sends guarded F24 input
    after 45 idle seconds, reduced for shorter detected lock timeouts.

    Monday-Friday, from 08:00 inclusive to 18:00 exclusive in America/Chicago,
    except excluded holidays, the input mode changes to a small mouse movement
    and return after 30 idle seconds (or a shorter detected lock threshold).
    This uses the original script's mouse-activity approach, with absolute
    virtual-desktop coordinates, no clicks, and paired move/return events.
    It aims to keep Teams Available but does not read or guarantee its badge.

    Outside that window, only the continuous F24 guard remains. Any Teams
    status is acceptable then; the script does not force Away. F24 itself may
    keep an activity-sensitive app Available. Neither the schedule nor a
    holiday releases the system/display request or switches off the idle guard.

    Both modes are simulated input. Real activity postpones pulses. Input is
    skipped on locked, secure, disconnected, or unavailable desktops and when
    modifiers, F24, or mouse buttons are held. The script never unlocks a PC,
    dismisses UAC, sends input to a login screen, types text, or changes policy.
    F24 can trigger a configured shortcut; mouse activity can affect hover UI.

CENTRAL TIME AND HOLIDAYS
    Scheduling and log timestamps use Central Time independently of the host
    time zone. IANA America/Chicago data is used when installed. On a standard
    Windows Python installation without that database, the script applies US
    rules in effect since 2007: second Sunday in March to first Sunday in
    November, switching between CST and CDT. No tzdata download is needed.
    This fallback must be updated if daylight-saving legislation changes.

    The exclusion calendar combines nationwide federal holidays (including
    Friday/Monday observance for weekend holidays) with Texas statutory dates:
    January 19, March 2, April 21, June 19, August 27, the day after Thanksgiving,
    December 24 and December 26. Texas-only weekend dates are not moved.
    This is a broad Texas + federal calendar, not one employer's closure list.
    Optional religious/personal holidays are not automatically excluded. Use
    repeated --holiday YYYY-MM-DD options for additional dates. --list-holidays
    shows the computed calendar, including cross-year federal observance.

TEAMS LIMITATIONS
    Local Windows activity can reduce inactivity-based Away status. It cannot
    override every Teams rule, meeting, call, calendar entry, manual status,
    disconnected account, or organization policy. If Teams is manually pinned
    to Away/Offline, use its Reset status option; this script does not change
    that selection. Green status remains best effort, never a verified promise.

    If the screen locks, Teams can become Away. The script will pause input
    until you return to the normal desktop; it never attempts to unlock it.
    --diagnose reads local idle, desktop, power-related settings and current
    schedule eligibility without acquiring a power request or sending input.
    Successful Windows idle-reset verification is not Teams-status validation.

CONTROL, OUTPUT, AND SAFETY
    --work-idle-seconds controls the work-hours threshold (default 30 seconds).
    --idle-seconds controls the continuous guard threshold (default 45 seconds).
    Shorter detected screen-saver/machine-lock limits reduce either threshold.
    --work-input f24 selects the original keyboard mechanism during work hours.
    --allow-lock explicitly disables ALL input, leaving system/display power
    requests only; inactivity locking is then possible, at any time of day.
    --once only acquires/releases power and exits; it does not test the guard.
    --dry-run previews on any OS without Windows DLLs or native side effects.

    Normal output: startup, schedule changes, one five-minute heartbeat, and
    successful-release shutdown. Pauses and failures are reported; --verbose
    adds native details. Sleeps between checks avoid a busy loop. Input reset
    verification uses a short processing grace period, and failures stop the
    process with an explanation. Power requests are released on normal/error
    exit and handled Ctrl+C. Forced termination cannot execute Python cleanup.

    Run only where you are authorized to keep the device unlocked. Explicit
    lock/sleep, lid actions, Dynamic Lock, presence sensing, critical battery,
    and enforced policy can still override requests. No script can promise
    this PC will never lock or sleep. No permanent power/registry/security
    settings, startup entries, or services are changed. Wakefulness consumes
    power; stop the program or use --duration when it is no longer needed.

    Stop the old process and restart this file. Startup must show v3.1.0;
    replacing a file cannot update a running Python process. Run this file
    instead of the original keep_awake.py or another idle utility.
    Exit codes: success 0, runtime failure 1, invalid arguments 2, interrupt 130.

TESTS AND REFERENCES
    python -m unittest discover -s tests -v
    Tests are separate; --self-test and former Graph options are unsupported.
    Automated checks do not establish actual Teams/lock behavior on your PC.
    https://comptroller.texas.gov/about/holidays.php
    https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/
    https://www.nist.gov/pml/time-and-frequency-division/popular-links/daylight-saving-time-dst
    https://learn.microsoft.com/en-us/microsoftteams/presence-admins
    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-mouseinput
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import datetime as dt
from functools import lru_cache
import logging
import math
import os
import signal
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "3.1.0"
LOGGER = logging.getLogger("keep_awake_teams")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
WAIT_SECONDS = 30.0
HEARTBEAT_SECONDS = 300.0
IDLE_SECONDS = 45.0
WORK_IDLE_SECONDS = 30.0
try:
    CENTRAL_ZONE = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    CENTRAL_ZONE = None  # Standard Windows Python may have no IANA time-zone database.
VK_F24 = 0x87
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
SPI_GETSCREENSAVEACTIVE = 0x0010
SPI_GETSCREENSAVETIMEOUT = 0x000E
SPI_GETBLOCKSENDINPUTRESETS = 0x1026
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
UOI_IO = 6
GUARD_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C, VK_F24, 0x01, 0x02, 0x04, 0x05, 0x06)

# Windows uses 32-bit LONG/DWORD even on x64; c_long is 64-bit on other hosts.
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
ULONG_PTR = ctypes.c_size_t


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("dwTime", DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", DWORD), ("data", INPUTUNION)]


@dataclass(frozen=True)
class InputState:
    tick: int
    idle_seconds: float


@dataclass(frozen=True)
class IdleSettings:
    screen_saver_seconds: float | None = None
    machine_lock_seconds: float | None = None
    blocks_synthetic_resets: bool | None = None


def nth_weekday(year, month, weekday, number):
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (number - 1))


def central_time(instant: dt.datetime) -> dt.datetime:
    """Independent of the computer's selected time zone; no downloaded dependency."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Central conversion requires an aware datetime")
    if CENTRAL_ZONE is not None:
        return instant.astimezone(CENTRAL_ZONE)
    utc = instant.astimezone(dt.timezone.utc)
    if utc.year < 2007:
        raise ValueError("Central time fallback supports current US rules from 2007 onward")
    start = dt.datetime.combine(nth_weekday(utc.year, 3, 6, 2), dt.time(8), dt.timezone.utc)
    end = dt.datetime.combine(nth_weekday(utc.year, 11, 6, 1), dt.time(7), dt.timezone.utc)
    daylight = start <= utc < end
    return utc.astimezone(
        dt.timezone(dt.timedelta(hours=-5 if daylight else -6), "CDT" if daylight else "CST")
    )


@lru_cache(maxsize=8)
def texas_holidays(year):
    """Texas statutory dates plus nationwide federal holidays and observed dates."""
    result = {}
    for source_year in (year - 1, year, year + 1):
        federal = {
            dt.date(source_year, 1, 1): "New Year's Day",
            nth_weekday(source_year, 1, 0, 3): "Martin Luther King Jr. Day",
            nth_weekday(source_year, 2, 0, 3): "Washington's Birthday",
            dt.date(source_year, 5, 31)
            - dt.timedelta(days=dt.date(source_year, 5, 31).weekday()): "Memorial Day",
            dt.date(source_year, 7, 4): "Independence Day",
            nth_weekday(source_year, 9, 0, 1): "Labor Day",
            nth_weekday(source_year, 10, 0, 2): "Columbus Day",
            dt.date(source_year, 11, 11): "Veterans Day",
            nth_weekday(source_year, 11, 3, 4): "Thanksgiving Day",
            dt.date(source_year, 12, 25): "Christmas Day",
        }
        if source_year >= 2021:
            federal[dt.date(source_year, 6, 19)] = "Juneteenth"
        for day, name in federal.items():
            if day.year == year:
                result[day] = name
            observed = day + dt.timedelta(
                days=-1 if day.weekday() == 5 else 1 if day.weekday() == 6 else 0
            )
            if observed.year == year:
                result[observed] = name + (" (observed)" if observed != day else "")
    for month, day, name in (
        (1, 19, "Confederate Heroes Day"),
        (3, 2, "Texas Independence Day"),
        (4, 21, "San Jacinto Day"),
        (6, 19, "Emancipation Day in Texas"),
        (8, 27, "Lyndon B. Johnson Day"),
        (12, 24, "Christmas Eve"),
        (12, 26, "Day after Christmas"),
    ):
        date = dt.date(year, month, day)
        result[date] = name if date not in result else result[date] + " / " + name
    result[nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1)] = "Day after Thanksgiving"
    return result


def work_period(instant, extra_holidays=frozenset()):
    central = central_time(instant)
    day = central.date()
    if day in extra_holidays:
        return False, "additional holiday"
    if day in texas_holidays(day.year):
        return False, texas_holidays(day.year)[day]
    if central.weekday() >= 5:
        return False, "weekend"
    return (
        (True, "work hours") if 8 <= central.hour < 18 else (False, "outside 08:00-18:00 Central")
    )


class CentralFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return central_time(dt.datetime.fromtimestamp(record.created, dt.timezone.utc)).strftime(
            datefmt or "%Y-%m-%d %H:%M:%S %Z"
        )


def choose_idle_seconds(requested: float, settings: IdleSettings) -> float:
    limits = [
        value
        for value in (settings.screen_saver_seconds, settings.machine_lock_seconds)
        if value is not None and value > 0 and math.isfinite(value)
    ]
    return min(requested, min(limits) / 2) if limits else requested


def configure_logging(verbose=False) -> None:
    """Use current stdout without disrupting root or third-party handlers."""
    for handler in list(LOGGER.handlers):
        if getattr(handler, "_keep_awake_console", False):
            LOGGER.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(sys.stdout)
    handler._keep_awake_console = True
    handler.setFormatter(
        CentralFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S %Z")
    )
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return seconds


def idle_threshold(value: str) -> float:
    seconds = positive_seconds(value)
    if not 1 <= seconds <= 300:
        raise argparse.ArgumentTypeError("idle threshold must be between 1 and 300 seconds")
    return seconds


def holiday_date(value):
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("holiday must use YYYY-MM-DD") from None


def machine_lock_seconds() -> float | None:
    """Read the configured machine inactivity limit without changing policy."""
    import winreg

    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            value, kind = winreg.QueryValueEx(key, "InactivityTimeoutSecs")
            if kind == winreg.REG_DWORD and value > 0:
                return float(value)
    except OSError:
        pass
    return None


class WindowsPlatform:
    """Standard Win32 power requests and guarded, transparent F24 input."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("live mode requires Windows; use --dry-run here")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        signatures = (
            (self.kernel32.SetThreadExecutionState, [DWORD], DWORD),
            (self.kernel32.GetTickCount, [], DWORD),
            (self.user32.GetLastInputInfo, [ctypes.POINTER(LASTINPUTINFO)], BOOL),
            (
                self.user32.SendInput,
                [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int],
                ctypes.c_uint,
            ),
            (
                self.user32.SystemParametersInfoW,
                [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint],
                BOOL,
            ),
            (self.user32.OpenInputDesktop, [DWORD, BOOL, DWORD], ctypes.c_void_p),
            (
                self.user32.GetUserObjectInformationW,
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)],
                BOOL,
            ),
            (self.user32.CloseDesktop, [ctypes.c_void_p], BOOL),
            (self.user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_int16),
            (self.user32.GetCursorPos, [ctypes.POINTER(POINT)], BOOL),
            (self.user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int),
        )
        for function, args, result in signatures:
            function.argtypes = args
            function.restype = result

    def set_keep_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if enabled else 0)
        LOGGER.debug("SetThreadExecutionState flags=0x%08X", flags)
        if not self.kernel32.SetThreadExecutionState(flags):
            raise OSError("Windows rejected the system/display power request")

    def get_input_state(self) -> InputState:
        last = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not self.user32.GetLastInputInfo(ctypes.byref(last)):
            raise OSError("Cannot read session idle time; idle-lock prevention is unavailable")
        elapsed_ms = (self.kernel32.GetTickCount() - last.dwTime) & 0xFFFFFFFF
        return InputState(last.dwTime, elapsed_ms / 1000.0)

    def _read_setting(self, action: int) -> int | None:
        value = DWORD()
        if self.user32.SystemParametersInfoW(action, 0, ctypes.byref(value), 0):
            return value.value
        return None

    def read_idle_settings(self) -> IdleSettings:
        active = self._read_setting(SPI_GETSCREENSAVEACTIVE)
        timeout = self._read_setting(SPI_GETSCREENSAVETIMEOUT) if active else None
        blocked = self._read_setting(SPI_GETBLOCKSENDINPUTRESETS)
        return IdleSettings(
            timeout if timeout and timeout > 0 else None,
            machine_lock_seconds(),
            None if blocked is None else bool(blocked),
        )

    def desktop_available(self) -> bool:
        desktop = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            return False
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = DWORD()
            receiving_input = BOOL()
            if not self.user32.GetUserObjectInformationW(
                desktop, UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)
            ):
                return False
            if not self.user32.GetUserObjectInformationW(
                desktop,
                UOI_IO,
                ctypes.byref(receiving_input),
                ctypes.sizeof(receiving_input),
                ctypes.byref(needed),
            ):
                return False
            return name.value.casefold() == "default" and bool(receiving_input.value)
        finally:
            self.user32.CloseDesktop(desktop)

    def held_keys(self) -> bool:
        return any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in GUARD_KEYS)

    def _mouse_events(self):
        point = POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("Cannot read pointer position for work-hours mouse activity")
        left, top, width, height = [
            self.user32.GetSystemMetrics(index) for index in (76, 77, 78, 79)
        ]
        if (
            width < 2
            or height < 1
            or not left <= point.x < left + width
            or not top <= point.y < top + height
        ):
            raise OSError("Cannot determine valid virtual-desktop bounds for mouse activity")
        target_x = point.x + (1 if point.x < left + width - 1 else -1)
        events = (INPUT * 2)()
        for event, x in zip(events, (target_x, point.x)):
            event.type = INPUT_MOUSE
            # Use absolute pixel centers to avoid pointer acceleration and return drift.
            event.mi.dx = min(65535, int((x - left + 0.5) * 65536 / width))
            event.mi.dy = min(65535, int((point.y - top + 0.5) * 65536 / height))
            event.mi.dwFlags = (
                MOUSEEVENTF_MOVE
                | MOUSEEVENTF_MOVE_NOCOALESCE
                | MOUSEEVENTF_VIRTUALDESK
                | MOUSEEVENTF_ABSOLUTE
            )
        return events

    def send_idle_pulse(self, minimum_idle_seconds=0.0, mode="f24") -> bool:
        # Recheck just before SendInput; never switch desktops or clear held keys.
        if not self.desktop_available() or self.held_keys():
            return False
        if minimum_idle_seconds and self.get_input_state().idle_seconds < minimum_idle_seconds:
            return False
        if mode == "mouse":
            events = self._mouse_events()
        elif mode == "f24":
            events = (INPUT * 2)()
            for event in events:
                event.type = INPUT_KEYBOARD
                event.ki.wVk = VK_F24
            events[1].ki.dwFlags = KEYEVENTF_KEYUP
        else:
            raise ValueError("input mode must be mouse or f24")
        sent = self.user32.SendInput(2, events, ctypes.sizeof(INPUT))
        if sent != 2:
            release_note = ""
            if sent == 1:
                # Undo only the first inserted event: key-up or pointer return.
                release = (INPUT * 1)(events[1])
                if self.user32.SendInput(1, release, ctypes.sizeof(INPUT)) != 1:
                    release_note = (
                        "; F24 release also failed"
                        if mode == "f24"
                        else "; mouse return also failed"
                    )
            raise OSError(
                "SendInput accepted %s/2 events%s; desktop or integrity restrictions may block the idle guard. Run --diagnose"
                % (sent, release_note)
            )
        LOGGER.debug("Sent %s pulse; waiting for Windows idle-reset verification", mode)
        return True


class KeepAwakeRunner:
    def __init__(
        self,
        platform=None,
        duration=None,
        dry_run=False,
        monotonic=None,
        sleep=None,
        heartbeat_seconds=HEARTBEAT_SECONDS,
        idle_seconds=IDLE_SECONDS,
        allow_lock=False,
        now_utc=None,
        work_idle_seconds=WORK_IDLE_SECONDS,
        work_input="mouse",
        extra_holidays=frozenset(),
    ):
        self.platform = platform
        self.duration = duration
        self.dry_run = dry_run
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_seconds = idle_seconds
        self.allow_lock = allow_lock
        self.now_utc = now_utc or (lambda: dt.datetime.now(dt.timezone.utc))
        self.work_idle_seconds = work_idle_seconds
        self.work_input = work_input
        self.extra_holidays = frozenset(extra_holidays)

    def run(self, once=False) -> int:
        guard_enabled = not self.allow_lock and not once
        if self.dry_run:
            LOGGER.info(
                "DRY RUN v%s: system/display keep-awake; %s; %s; no native calls made",
                VERSION,
                "24/7 F24 idle guard; work-hours %s activity Mon-Fri 08:00-18:00 Central, except Texas/federal holidays"
                % self.work_input
                if guard_enabled
                else "power only; inactivity lock possible",
                "until stopped" if self.duration is None else "duration %.3gs" % self.duration,
            )
            active, reason = work_period(self.now_utc(), self.extra_holidays)
            LOGGER.info(
                "Work-hours activity now: %s (%s); Teams badge is unverified",
                "eligible" if active and guard_enabled else "off",
                reason,
            )
            return 0

        threshold = self.idle_seconds
        if guard_enabled:
            settings = self.platform.read_idle_settings()
            if settings.blocks_synthetic_resets:
                raise OSError(
                    "Windows blocks simulated-input screen-saver resets. Idle-lock prevention cannot be provided; use --diagnose or --allow-lock for power only"
                )
            threshold = choose_idle_seconds(threshold, settings)
        base_threshold = threshold
        acquired = False
        try:
            self.platform.set_keep_awake(True)
            acquired = True
            started = self.monotonic()
            deadline = started + self.duration if self.duration is not None else None
            next_heartbeat = started + self.heartbeat_seconds
            LOGGER.info(
                "Running v%s: system/display keep-awake requested; %s; Ctrl+C stops",
                VERSION,
                "24/7 idle guard after %.3gs; weekday Teams activity 08:00-18:00 Central"
                % threshold
                if guard_enabled
                else "power check only"
                if once
                else "idle guard off; inactivity lock possible",
            )
            if once:
                return 0
            pending_tick = None
            verify_by = None
            paused_reason = None
            pulses = 0
            last_period = None
            pending_threshold = threshold
            while True:
                current = self.monotonic()
                if deadline is not None and current >= deadline:
                    return 0
                reason = None
                period = (
                    work_period(self.now_utc(), self.extra_holidays)
                    if guard_enabled
                    else (False, "idle guard disabled")
                )
                work_active, work_reason = period
                mode = self.work_input if work_active else "f24"
                threshold = (
                    min(base_threshold, self.work_idle_seconds) if work_active else base_threshold
                )
                poll_seconds = min(5.0, threshold / 3) if guard_enabled else WAIT_SECONDS
                if period != last_period and guard_enabled:
                    LOGGER.info(
                        "Work-hours activity %s: %s; %s after %.3gs idle; wakefulness continues; Teams badge unverified",
                        "enabled" if work_active else "off",
                        work_reason,
                        mode,
                        threshold,
                    )
                    last_period = period
                if guard_enabled:
                    if not self.platform.desktop_available():
                        pending_tick = None
                        reason = "desktop locked, secure, disconnected, or unavailable"
                    else:
                        state = self.platform.get_input_state()
                        if pending_tick is not None:
                            if (
                                state.tick != pending_tick
                                and state.idle_seconds < pending_threshold
                            ):
                                pulses += 1
                                pending_tick = None
                                LOGGER.debug(
                                    "Windows reported an idle reset; confirmed pulses=%d", pulses
                                )
                            elif current >= verify_by:
                                raise OSError(
                                    "Windows did not confirm an idle reset after the input pulse; idle-lock prevention is unavailable. Run --diagnose"
                                )
                        if pending_tick is None and state.idle_seconds >= threshold:
                            if self.platform.held_keys():
                                reason = "a modifier, F24, or mouse button is held"
                            elif self.platform.send_idle_pulse(threshold, mode=mode):
                                pending_tick = state.tick
                                pending_threshold = threshold
                                verify_by = self.monotonic() + min(2.0, threshold / 2)
                            else:
                                reason = "desktop or user-input state changed before the pulse"
                if reason != paused_reason:
                    if reason is not None:
                        LOGGER.warning(
                            "Idle guard paused: %s; inactivity locking remains possible", reason
                        )
                    elif paused_reason is not None:
                        LOGGER.info("Idle guard resumed on the normal input desktop")
                    paused_reason = reason
                if current >= next_heartbeat:
                    LOGGER.info(
                        "Still running (%.0fs); system/display request held; idle guard %s; confirmed resets=%d; work activity %s; Teams badge unverified",
                        current - started,
                        "paused" if paused_reason else "enabled" if guard_enabled else "off",
                        pulses,
                        "enabled" if work_active else "off",
                    )
                    next_heartbeat = current + self.heartbeat_seconds
                after_checks = self.monotonic()
                delay = min(poll_seconds, max(0.0, next_heartbeat - after_checks))
                if pending_tick is not None:
                    delay = min(delay, 1.0, max(0.0, verify_by - after_checks))
                if deadline is not None:
                    if after_checks >= deadline:
                        return 0
                    delay = min(delay, deadline - after_checks)
                self.sleep(delay)
        finally:
            if acquired:
                self.platform.set_keep_awake(False)
                LOGGER.info("Stopped: system/display power request released")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="24/7 Windows wakefulness; weekday 08:00-18:00 Central Teams activity without API authentication.",
        epilog="Mouse/F24 activity is simulated input; green Teams status is best effort. Use --docs for limits.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--duration",
        type=positive_seconds,
        metavar="SECONDS",
        help="stop after elapsed seconds (default: until Ctrl+C)",
    )
    parser.add_argument(
        "--idle-seconds",
        type=idle_threshold,
        default=IDLE_SECONDS,
        metavar="SECONDS",
        help="idle threshold, 1-300s (default: 45s; reduced for shorter known timeouts)",
    )
    parser.add_argument(
        "--work-idle-seconds",
        type=idle_threshold,
        default=WORK_IDLE_SECONDS,
        metavar="SECONDS",
        help="idle threshold during work hours, 1-300s (default: 30)",
    )
    parser.add_argument(
        "--work-input",
        choices=("mouse", "f24"),
        default="mouse",
        help="work-hours activity mechanism (default: mouse); outside work hours F24 continues",
    )
    parser.add_argument(
        "--holiday",
        type=holiday_date,
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="additional employer or personal holiday; repeat for multiple dates",
    )
    parser.add_argument(
        "--list-holidays",
        type=int,
        metavar="YEAR",
        help="print the built-in exclusion calendar and exit without Windows calls",
    )
    parser.add_argument(
        "--allow-lock",
        action="store_true",
        help="system/display power only; no input, so inactivity lock remains possible",
    )
    parser.add_argument(
        "--teams",
        choices=("off",),
        default="off",
        help="compatibility option: no Teams APIs; F24 may still affect presence",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preview and exit on any OS without native calls"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="only acquire/release the power request; no input or idle-guard test",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="read Windows idle/desktop settings without input or power changes",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include native requests and idle-reset verification details",
    )
    parser.add_argument(
        "--docs", action="store_true", help="print behavior, diagnosis, usage, and limitations"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def diagnose(platform, requested_idle=IDLE_SECONDS, extra_holidays=frozenset()) -> None:
    settings = platform.read_idle_settings()
    state = platform.get_input_state()
    print("Windows keep-awake v%s diagnosis (read only)" % VERSION)
    now = dt.datetime.now(dt.timezone.utc)
    active, reason = work_period(now, extra_holidays)
    print("Central time: %s" % central_time(now).strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("Work-hours activity eligible: %s (%s)" % (active, reason))
    print("Normal desktop receiving input: %s" % platform.desktop_available())
    print("Session idle: %.3gs" % state.idle_seconds)
    print(
        "Screen-saver timeout: %s"
        % (
            "disabled or unreadable"
            if settings.screen_saver_seconds is None
            else "%.3gs" % settings.screen_saver_seconds
        )
    )
    print(
        "Machine inactivity limit: %s"
        % (
            "disabled or unreadable"
            if settings.machine_lock_seconds is None
            else "%.3gs" % settings.machine_lock_seconds
        )
    )
    print(
        "Windows blocks synthetic screen-saver resets: %s"
        % (
            "unknown"
            if settings.blocks_synthetic_resets is None
            else settings.blocks_synthetic_resets
        )
    )
    print("Chosen idle guard threshold: %.3gs" % choose_idle_seconds(requested_idle, settings))
    print(
        "Mouse/F24 input may affect Teams presence; no badge is read or guaranteed. Other lock policies may still apply."
    )


def stop(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.docs:
        print(__doc__)
        return 0
    if args.list_holidays is not None:
        if not 2007 <= args.list_holidays <= 9998:
            parser.error("--list-holidays year must be between 2007 and 9998")
        holidays = dict(texas_holidays(args.list_holidays))
        holidays.update(
            {day: "additional holiday" for day in args.holiday if day.year == args.list_holidays}
        )
        print(
            "Texas state + federal exclusion dates; federal weekend observance; Texas-only dates unshifted"
        )
        for day, name in sorted(holidays.items()):
            print("%s %s: %s" % (day.isoformat(), day.strftime("%A"), name))
        return 0
    if args.diagnose and (args.dry_run or args.once):
        parser.error("--diagnose cannot be combined with --dry-run or --once")
    configure_logging(args.verbose)
    previous_sigterm = None
    signal_installed = False
    try:
        platform = None if args.dry_run else WindowsPlatform()
        if args.diagnose:
            diagnose(platform, args.idle_seconds, args.holiday)
            return 0
        if not args.dry_run:
            previous_sigterm = signal.signal(signal.SIGTERM, stop)
            signal_installed = True
        return KeepAwakeRunner(
            platform,
            args.duration,
            args.dry_run,
            idle_seconds=args.idle_seconds,
            allow_lock=args.allow_lock,
            work_idle_seconds=args.work_idle_seconds,
            work_input=args.work_input,
            extra_holidays=args.holiday,
        ).run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.debug("Interrupted; exiting with status 130")
        return 130
    except (OSError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1
    finally:
        if signal_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
