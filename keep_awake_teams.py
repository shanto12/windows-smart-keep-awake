#!/usr/bin/env python3
"""Keep an authorized Windows desktop awake during an unattended task.

Python 3.9+, standard library only. Run in your normal signed-in Windows session:
    py keep_awake_teams.py
    py keep_awake_teams.py --duration 3600
    py keep_awake_teams.py --diagnose
    py keep_awake_teams.py --dry-run
    py keep_awake_teams.py --allow-lock
    py keep_awake_teams.py --docs

WHAT CHANGED IN VERSION 3
    Version 2 only requested system wakefulness; it intentionally allowed the
    display, screen saver, and inactivity lock to activate. Version 3 also
    requests display wakefulness and enables an idle guard by default.

    The guard checks Windows' session idle time. After 45 idle seconds, or half
    a shorter detected screen-saver/machine-lock timeout, it sends one F24
    key-down/key-up pair using SendInput. It sends nothing while user activity
    keeps the idle time below that threshold. It checks that Windows reports
    new input afterward and stops with an error if verification fails.

    F24 does not type text or move the pointer, but it IS simulated keyboard
    input. Applications can bind it to an action, and activity-sensitive apps
    such as Teams may count it as activity. This version cannot also promise
    natural Away status while idle. It never calls a Teams API, signs in,
    requests credentials, or claims to verify your Teams badge.

HOW IT OPERATES
    A thread-scoped SetThreadExecutionState request holds both the system and
    display awake. All native calls run on the same thread. The guard uses
    short sleeping waits (normally five seconds), not a busy loop or constant
    keystrokes. Real keyboard/mouse activity automatically postpones its pulse.
    Readable lock timeouts are inspected at startup without changing settings.

    Input pauses on a locked, secure, disconnected, or unavailable desktop and
    while Shift/Ctrl/Alt/Windows, F24, or a mouse button is held. It resumes on
    the normal input desktop. It does not unlock, dismiss UAC, inject into a
    login screen, change a power plan, or alter any registry/security policy.

    A normal run prints its version and selected behavior, a status heartbeat
    every five minutes, and a shutdown message after releasing the request.
    Pauses and failures are reported when they occur. --verbose adds diagnostic
    details. All log timestamps use the machine's local time.

LIMITS AND DIAGNOSIS
    Use only where you are authorized to keep the device unlocked. Windows or
    organization policy can block synthetic idle resets or override power
    requests. Explicit Win+L, remote lock, Dynamic Lock, presence sensing,
    forced sleep, lid actions, shutdown, and critical battery actions remain
    possible. This program cannot guarantee that Windows will never lock or
    sleep. A running heartbeat alone is not proof of an unlocked desktop.

    --diagnose reads desktop availability, idle time, the screen-saver timeout,
    the machine inactivity limit, and the synthetic-reset restriction without
    sending input or acquiring a power request. If Windows explicitly blocks
    synthetic screen-saver resets, the default mode exits with an explanation;
    --allow-lock remains available for system/display wakefulness only.
    Some MDM, remote-session, and presence-based limits are not discoverable.
    --idle-seconds can lower the fallback threshold for a known shorter limit.

    --allow-lock never sends input; inactivity locking remains possible.
    --once only checks power-request acquisition/release, then exits; it does
    NOT exercise the idle guard or prove that future locking is prevented.
    --dry-run works on any OS and loads no Windows DLLs. --duration is elapsed
    time measured by a monotonic clock. Ctrl+C releases the power request;
    force termination cannot execute Python cleanup. Exit codes: success 0,
    runtime failure 1, invalid arguments 2, handled interruption 130.

    Stop the old process and start this updated file; editing/downloading a
    file does not update an already-running Python process. The startup line
    must say v3.0.0. Run this file instead of the original keep_awake.py or
    another idle utility. Nothing is installed and no administrator elevation
    or third-party packages are required. Keeping a display and system awake
    consumes battery; use --duration or stop the program when it is not needed.

TESTS
    Tests remain separate: python -m unittest discover -s tests -v
    --self-test and former Graph/scheduling options are unsupported.
    Offline tests and hosted CI do not establish behavior on your Windows PC.
    Verify there by leaving it unattended beyond its usual lock timeout while
    watching for guard warnings; use --diagnose if it still locks.

MICROSOFT API REFERENCES
    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-systemparametersinfow
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import logging
import math
import os
import signal
import sys
import time

VERSION = "3.0.0"
LOGGER = logging.getLogger("keep_awake_teams")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
WAIT_SECONDS = 30.0
HEARTBEAT_SECONDS = 300.0
IDLE_SECONDS = 45.0
VK_F24 = 0x87
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
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
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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

    def send_idle_pulse(self, minimum_idle_seconds=0.0) -> bool:
        # Recheck just before SendInput; never switch desktops or clear held keys.
        if not self.desktop_available() or self.held_keys():
            return False
        if minimum_idle_seconds and self.get_input_state().idle_seconds < minimum_idle_seconds:
            return False
        events = (INPUT * 2)()
        for event in events:
            event.type = INPUT_KEYBOARD
            event.ki.wVk = VK_F24
        events[1].ki.dwFlags = KEYEVENTF_KEYUP
        sent = self.user32.SendInput(2, events, ctypes.sizeof(INPUT))
        if sent != 2:
            release_note = ""
            if sent == 1:
                # A partial insert may leave our F24 down. Attempt only its release.
                release = (INPUT * 1)(events[1])
                if self.user32.SendInput(1, release, ctypes.sizeof(INPUT)) != 1:
                    release_note = "; F24 release also failed"
            raise OSError(
                "SendInput accepted %s/2 events%s; desktop or integrity restrictions may block the idle guard. Run --diagnose"
                % (sent, release_note)
            )
        LOGGER.debug("Sent F24 down/up; waiting for Windows idle-reset verification")
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
    ):
        self.platform = platform
        self.duration = duration
        self.dry_run = dry_run
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_seconds = idle_seconds
        self.allow_lock = allow_lock

    def run(self, once=False) -> int:
        guard_enabled = not self.allow_lock and not once
        if self.dry_run:
            LOGGER.info(
                "DRY RUN v%s: system/display keep-awake; %s; %s; no native calls made",
                VERSION,
                "F24 idle guard (may affect Teams presence)"
                if guard_enabled
                else "power only; inactivity lock possible",
                "until stopped" if self.duration is None else "duration %.3gs" % self.duration,
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
        poll_seconds = min(5.0, threshold / 3) if guard_enabled else WAIT_SECONDS
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
                "F24 idle guard after %.3gs (may affect Teams presence)" % threshold
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
            while True:
                current = self.monotonic()
                if deadline is not None and current >= deadline:
                    return 0
                reason = None
                if guard_enabled:
                    if not self.platform.desktop_available():
                        pending_tick = None
                        reason = "desktop locked, secure, disconnected, or unavailable"
                    else:
                        state = self.platform.get_input_state()
                        if pending_tick is not None:
                            if state.tick != pending_tick and state.idle_seconds < threshold:
                                pulses += 1
                                pending_tick = None
                                LOGGER.debug(
                                    "Windows reported an idle reset; confirmed pulses=%d", pulses
                                )
                            elif current >= verify_by:
                                raise OSError(
                                    "Windows did not confirm an idle reset after F24; idle-lock prevention is unavailable. Run --diagnose"
                                )
                        if pending_tick is None and state.idle_seconds >= threshold:
                            if self.platform.held_keys():
                                reason = "a modifier, F24, or mouse button is held"
                            elif self.platform.send_idle_pulse(threshold):
                                pending_tick = state.tick
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
                        "Still running (%.0fs); system/display request held; idle guard %s; confirmed resets=%d",
                        current - started,
                        "paused" if paused_reason else "enabled" if guard_enabled else "off",
                        pulses,
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
        description="Keep Windows system/display awake with a verified F24 idle guard.",
        epilog="F24 is simulated input and may affect Teams presence. Use --docs for limits.",
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


def diagnose(platform, requested_idle=IDLE_SECONDS) -> None:
    settings = platform.read_idle_settings()
    state = platform.get_input_state()
    print("Windows keep-awake v%s diagnosis (read only)" % VERSION)
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
    print("F24 can affect app shortcuts and Teams presence. Other lock policies may still apply.")


def stop(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.docs:
        print(__doc__)
        return 0
    if args.diagnose and (args.dry_run or args.once):
        parser.error("--diagnose cannot be combined with --dry-run or --once")
    configure_logging(args.verbose)
    previous_sigterm = None
    signal_installed = False
    try:
        platform = None if args.dry_run else WindowsPlatform()
        if args.diagnose:
            diagnose(platform, args.idle_seconds)
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
        ).run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.debug("Interrupted; exiting with status 130")
        return 130
    except OSError as error:
        LOGGER.error("%s", error)
        return 1
    finally:
        if signal_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
