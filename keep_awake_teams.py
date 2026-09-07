#!/usr/bin/env python3
"""Keep Windows awake while leaving Teams presence to genuine user activity.

Python 3.9+, standard library only. No app registration, credentials, MSAL,
Microsoft Graph permission, or network connection is needed by this script.
Use only on a device you own or are authorized to manage and where permitted.

RUN (PowerShell on Windows)
    py keep_awake_teams.py
    py keep_awake_teams.py --duration 3600
    py keep_awake_teams.py --dry-run
    py keep_awake_teams.py --once --verbose
    py -m unittest discover -s tests -v
    py keep_awake_teams.py --docs

DEFAULT BEHAVIOR
    Request prevention of ordinary idle system sleep continuously until Ctrl+C
    or process exit. The single native operation is SetThreadExecutionState:
    ES_CONTINUOUS | ES_SYSTEM_REQUIRED on entry, ES_CONTINUOUS on exit, on the
    same thread. The thread waits quietly between stop/duration checks. There
    is no keyboard/mouse input, UI automation, Teams access, or idle-user pulse.

    The display can turn off, the screen saver can run, and Windows can lock
    normally. The script neither reads nor changes inactivity/lock policies,
    power plans, registry settings, or Teams configuration. It does not unlock
    the device or attempt to defeat explicit sleep, lid actions, shutdown, or
    device/organization policy. Windows can reject or override the request.

    Keeping the system awake consumes power. Microsoft advises avoiding
    indefinite requests, particularly on Modern Standby devices where battery
    drain can occur even with the lid closed. Stop it when wakefulness is no
    longer needed; --duration SECONDS provides an optional automatic stop.
    It cannot wake an already sleeping/off machine or survive process exit.
    It installs no service, startup task, or permanent setting.

TEAMS: NATURAL INACTIVITY, NOT A PRESENCE SCHEDULE
    Microsoft documents that Teams on a computer normally becomes Away after
    a few minutes of user inactivity or when the computer locks. Preventing
    ordinary system sleep without producing input leaves that normal behavior
    to Teams. This is an expectation based on the documented mechanisms, not
    a guarantee for every Teams version, device, or configuration.

    This program NEVER reads, sets, checks, or guarantees a Teams badge. It does
    not mark you Available while idle or force Away at a deadline. Calls,
    meetings, calendar entries, manual status, other signed-in devices, client
    behavior, policy, and propagation delays can affect the displayed status.
    A particular Away timeout or permanent green state cannot be promised.

    There is NO 08:00-18:00 rule, weekend rule, or public-holiday rule in this
    mode. Teams can become Away during business hours if you are inactive and
    can show you Available outside them if you are actually active. Exact
    scheduled availability is not achievable by this power-only mechanism.

MIGRATING FROM THE GRAPH VERSION OF THIS FILE
    This version replaces the former Graph implementation. All authentication,
    token handling, Teams writes, presence renewal, and schedule logic are
    removed. --teams off remains an optional compatibility spelling of the
    default. --teams graph and former --hours/holiday/credential/presence-mode
    options are rejected rather than silently promising unsupported behavior.

    Stop any earlier Graph-enabled process before starting this version. Merely
    replacing a Python file does not stop a process already running that file.
    The earlier version's five-minute Graph leases may take time to expire and
    propagate; this version makes no API request to clear or renew them.

    Run this file INSTEAD OF the repository's original keep_awake.py, whose
    default behavior generates keyboard input. This script does not import,
    start, modify, or stop that original program or any other utility. Other
    programs that produce input can still affect Windows/Teams inactivity.

VALIDATION AND CLI DETAILS
    A normal run prints an active confirmation, one heartbeat every five
    minutes, and a shutdown message after release to standard output (including
    PyCharm's Run console). Timestamps use local time. Heartbeats confirm this process
    is running, not the actual sleep state or any Teams badge. --verbose adds
    startup settings and native-call details, with no per-loop output.

    --dry-run prints the intended behavior and exits on any OS without loading
    Windows DLLs or changing anything. --once briefly requests sleep prevention
    and immediately releases it: it does NOT keep Windows awake after exit.
    --duration uses a monotonic clock, independent of local timezone/DST changes;
    it is not a wall-clock alarm or a way to wake the machine. Log timestamps
    use the machine's local time. Ctrl+C is handled with best-effort cleanup;
    force termination cannot run Python cleanup. An API error returns exit 1,
    invalid arguments exit 2, and a handled interruption exits 130.

    Offline tests live separately in tests/test_keep_awake_teams.py and run
    through the existing CI unittest discovery command. From the repository
    root, run: py -m unittest discover -s tests -v. The former --self-test
    option has been removed; the standalone runtime needs no test files.
    Automated tests cannot establish the actual badge displayed to colleagues
    or verify sleep behavior on your Windows laptop. Those require observation
    on that device with Teams left to run normally; this script does not perform
    that observation or automate Teams.

OFFICIAL MICROSOFT REFERENCES
    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
    https://learn.microsoft.com/en-us/windows/win32/power/system-sleep-criteria
    https://learn.microsoft.com/en-us/microsoftteams/presence-admins
    https://support.microsoft.com/en-us/teams/notifications-settings/change-your-status-in-microsoft-teams
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import math
import os
import signal
import sys
import time


LOGGER = logging.getLogger("keep_awake_teams")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
WAIT_SECONDS = 30.0
HEARTBEAT_SECONDS = 300.0


def configure_logging(verbose=False) -> None:
    """Use the current Run console even if an IDE already configured root logging."""
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
        raise argparse.ArgumentTypeError("duration must be a number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and greater than zero")
    return seconds


class WindowsPlatform:
    """Only a thread-scoped system power request; no input or lock APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("live mode requires Windows; use --dry-run here")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        self.kernel32.SetThreadExecutionState.restype = ctypes.c_uint32

    def set_keep_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        LOGGER.debug("SetThreadExecutionState flags=0x%08X", flags)
        if not self.kernel32.SetThreadExecutionState(flags):
            # This API does not document a useful GetLastError value.
            raise OSError("SetThreadExecutionState failed; Windows rejected the power request")


class KeepAwakeRunner:
    """Hold and release the request on the calling thread, regardless of time/day."""

    def __init__(
        self,
        platform=None,
        duration=None,
        dry_run=False,
        monotonic=None,
        sleep=None,
        heartbeat_seconds=HEARTBEAT_SECONDS,
    ):
        self.platform = platform
        self.duration = duration
        self.dry_run = dry_run
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.heartbeat_seconds = heartbeat_seconds

    def run(self, once=False) -> int:
        if self.dry_run:
            LOGGER.info(
                "DRY RUN: would request system sleep prevention %s; display/lock unchanged; "
                "no input or Teams access; Teams presence unobserved",
                "for one brief check"
                if once
                else (
                    "for %.3g seconds" % self.duration
                    if self.duration is not None
                    else "until stopped"
                ),
            )
            return 0

        acquired = False
        try:
            LOGGER.debug(
                "Run settings: duration=%s; heartbeat every %.0fs; Teams access off",
                "unlimited" if self.duration is None else "%.3gs" % self.duration,
                self.heartbeat_seconds,
            )
            self.platform.set_keep_awake(True)
            acquired = True
            started_at = self.monotonic()
            LOGGER.info(
                "Running: Windows sleep-prevention request active; %s",
                "one-shot check" if once else "press Ctrl+C to stop",
            )
            if once:
                LOGGER.debug("One-shot check complete; stopping")
                return 0
            deadline = started_at + self.duration if self.duration is not None else None
            next_heartbeat = started_at + self.heartbeat_seconds
            while True:
                current = self.monotonic()
                delay = WAIT_SECONDS
                if deadline is not None:
                    remaining = deadline - current
                    if remaining <= 0:
                        LOGGER.debug("Duration complete; stopping")
                        return 0
                    delay = min(delay, remaining)
                if current >= next_heartbeat:
                    LOGGER.info(
                        "Still running (%.0fs elapsed); Windows sleep prevention requested",
                        current - started_at,
                    )
                    # A late wake emits one heartbeat, never a catch-up burst.
                    next_heartbeat = current + self.heartbeat_seconds
                delay = min(delay, next_heartbeat - current)
                self.sleep(delay)
        finally:
            if acquired:
                self.platform.set_keep_awake(False)
                LOGGER.info("Stopped: Windows sleep-prevention request released")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keep Windows awake without input; leave Teams presence to genuine activity.",
        epilog="No credentials or third-party packages. Use --docs for limitations and migration.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--duration",
        type=positive_seconds,
        metavar="SECONDS",
        help="stop after this many elapsed seconds (default: until Ctrl+C)",
    )
    parser.add_argument(
        "--teams",
        choices=("off",),
        default="off",
        help="compatibility option; Teams access is always off",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preview and exit on any OS, with no native calls"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="briefly acquire and release the power request, then exit",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="add startup settings and native-call details"
    )
    parser.add_argument(
        "--docs", action="store_true", help="print setup, behavior, and limitations"
    )
    parser.add_argument("--version", action="version", version="2.0.0")
    return parser


def stop(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.docs:
        print(__doc__)
        return 0
    configure_logging(args.verbose)
    previous_sigterm = None
    signal_installed = False
    try:
        platform = None if args.dry_run else WindowsPlatform()
        if not args.dry_run:
            previous_sigterm = signal.signal(signal.SIGTERM, stop)
            signal_installed = True
        return KeepAwakeRunner(platform, args.duration, args.dry_run).run(once=args.once)
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
