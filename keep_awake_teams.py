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
    py keep_awake_teams.py --self-test
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
    --dry-run prints the intended behavior and exits on any OS without loading
    Windows DLLs or changing anything. --once briefly requests sleep prevention
    and immediately releases it: it does NOT keep Windows awake after exit.
    --duration uses a monotonic clock, independent of local timezone/DST changes;
    it is not a wall-clock alarm or a way to wake the machine. Log timestamps
    use the machine's local time. Ctrl+C is handled with best-effort cleanup;
    force termination cannot run Python cleanup. An API error returns exit 1,
    invalid arguments exit 2, and a handled interruption exits 130.

    --self-test runs the embedded offline tests using a fake Windows interface.
    Existing repository CI does not invoke them, so run --self-test explicitly.
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
            raise OSError("live mode requires Windows; --dry-run and --self-test work here")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        self.kernel32.SetThreadExecutionState.restype = ctypes.c_uint32

    def set_keep_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        if not self.kernel32.SetThreadExecutionState(flags):
            # This API does not document a useful GetLastError value.
            raise OSError("SetThreadExecutionState failed; Windows rejected the power request")


class KeepAwakeRunner:
    """Hold and release the request on the calling thread, regardless of time/day."""

    def __init__(self, platform=None, duration=None, dry_run=False, monotonic=None, sleep=None):
        self.platform = platform
        self.duration = duration
        self.dry_run = dry_run
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep

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
            self.platform.set_keep_awake(True)
            acquired = True
            LOGGER.info("Windows system-awake request active; Teams manages its own presence")
            if once:
                return 0
            deadline = self.monotonic() + self.duration if self.duration is not None else None
            while True:
                delay = WAIT_SECONDS
                if deadline is not None:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        return 0
                    delay = min(delay, remaining)
                self.sleep(delay)
        finally:
            if acquired:
                self.platform.set_keep_awake(False)
                LOGGER.info("Windows awake request released; normal power behavior resumes")


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
        "--verbose", action="store_true", help="log request acquisition and cleanup"
    )
    parser.add_argument(
        "--docs", action="store_true", help="print setup, behavior, and limitations"
    )
    parser.add_argument("--self-test", action="store_true", help="run embedded offline tests")
    parser.add_argument("--version", action="version", version="2.0.0")
    return parser


def stop(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.docs:
        print(__doc__)
        return 0
    if args.self_test:
        return run_self_tests()
    logging.basicConfig(
        level=logging.INFO if args.verbose or args.dry_run else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    previous_sigterm = None
    signal_installed = False
    try:
        platform = None if args.dry_run else WindowsPlatform()
        if not args.dry_run:
            previous_sigterm = signal.signal(signal.SIGTERM, stop)
            signal_installed = True
        return KeepAwakeRunner(platform, args.duration, args.dry_run).run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 130
    except OSError as error:
        LOGGER.error("%s", error)
        return 1
    finally:
        if signal_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)


def run_self_tests() -> int:
    """Keep tests in this file; no real Windows, input, or network operations."""
    import contextlib
    import io
    import unittest
    from unittest.mock import Mock, patch

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def set_keep_awake(self, enabled):
            self.calls.append(enabled)

    class RunnerTests(unittest.TestCase):
        def test_default_holds_request_across_multiple_waits(self):
            platform = FakePlatform()
            waits = []

            def sleep(delay):
                self.assertEqual(platform.calls, [True])
                waits.append(delay)
                if len(waits) == 3:
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                KeepAwakeRunner(platform, sleep=sleep).run()
            self.assertEqual(platform.calls, [True, False])
            self.assertEqual(waits, [WAIT_SECONDS] * 3)

        def test_duration_uses_elapsed_time_and_releases(self):
            platform = FakePlatform()
            elapsed = [100.0]
            waits = []

            def sleep(delay):
                self.assertEqual(platform.calls, [True])
                elapsed[0] += delay
                waits.append(delay)

            runner = KeepAwakeRunner(
                platform, duration=65, monotonic=lambda: elapsed[0], sleep=sleep
            )
            self.assertEqual(runner.run(), 0)
            self.assertEqual(waits, [30, 30, 5])
            self.assertEqual(platform.calls, [True, False])

        def test_fractional_duration(self):
            elapsed = [0.0]

            def sleep(delay):
                elapsed[0] += delay

            platform = FakePlatform()
            KeepAwakeRunner(
                platform, duration=0.25, monotonic=lambda: elapsed[0], sleep=sleep
            ).run()
            self.assertEqual(elapsed[0], 0.25)
            self.assertEqual(platform.calls, [True, False])

        def test_once_releases_without_waiting(self):
            platform = FakePlatform()
            sleep = Mock(side_effect=AssertionError("must not wait"))
            self.assertEqual(KeepAwakeRunner(platform, sleep=sleep).run(once=True), 0)
            self.assertEqual(platform.calls, [True, False])
            sleep.assert_not_called()

        def test_dry_run_has_no_native_calls_or_waits(self):
            platform = Mock()
            sleep = Mock(side_effect=AssertionError("must not wait"))
            self.assertEqual(KeepAwakeRunner(platform, dry_run=True, sleep=sleep).run(), 0)
            platform.set_keep_awake.assert_not_called()
            sleep.assert_not_called()

        def test_unexpected_error_still_releases_request(self):
            platform = FakePlatform()
            sleep = Mock(side_effect=RuntimeError("test failure"))
            with self.assertRaises(RuntimeError):
                KeepAwakeRunner(platform, sleep=sleep).run()
            self.assertEqual(platform.calls, [True, False])

        def test_rejected_acquisition_is_not_reported_active(self):
            platform = Mock()
            platform.set_keep_awake.side_effect = OSError("request rejected")
            with self.assertRaises(OSError):
                KeepAwakeRunner(platform).run(once=True)
            platform.set_keep_awake.assert_called_once_with(True)

        def test_cleanup_failure_is_reported(self):
            platform = Mock()
            platform.set_keep_awake.side_effect = [None, OSError("cleanup rejected")]
            with self.assertRaisesRegex(OSError, "cleanup rejected"):
                KeepAwakeRunner(platform).run(once=True)
            self.assertEqual(platform.set_keep_awake.call_count, 2)

    class NativeBoundaryTests(unittest.TestCase):
        def test_only_system_request_flags_and_cleanup(self):
            platform = WindowsPlatform.__new__(WindowsPlatform)
            platform.kernel32 = Mock()
            platform.kernel32.SetThreadExecutionState.return_value = 1
            platform.set_keep_awake(True)
            platform.set_keep_awake(False)
            self.assertEqual(
                [call.args[0] for call in platform.kernel32.SetThreadExecutionState.call_args_list],
                [0x80000001, 0x80000000],
            )
            self.assertEqual(set(platform.kernel32._mock_children), {"SetThreadExecutionState"})

        def test_zero_native_return_is_an_error(self):
            platform = WindowsPlatform.__new__(WindowsPlatform)
            platform.kernel32 = Mock()
            platform.kernel32.SetThreadExecutionState.return_value = 0
            with self.assertRaises(OSError):
                platform.set_keep_awake(True)

    class CliTests(unittest.TestCase):
        def test_default_is_no_teams_and_no_duration(self):
            args = build_parser().parse_args([])
            self.assertEqual(args.teams, "off")
            self.assertIsNone(args.duration)
            self.assertEqual(build_parser().parse_args(["--teams", "off"]).teams, "off")

        def test_invalid_duration_values(self):
            for value in ("nan", "inf", "-1", "0", "bad"):
                with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                    positive_seconds(value)

        def test_former_graph_and_schedule_options_are_rejected(self):
            for args in (
                ["--teams", "graph"],
                ["--hours", "08:00-18:00"],
                ["--holiday", "2026-12-25"],
                ["--client-id", "example"],
                ["--presence-mode", "preferred"],
            ):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as result,
                ):
                    build_parser().parse_args(args)
                self.assertEqual(result.exception.code, 2)

        def test_main_dry_run_does_not_load_windows_or_change_signal(self):
            module = sys.modules[__name__]
            with (
                patch.object(module, "WindowsPlatform") as platform,
                patch.object(signal, "signal") as handler,
            ):
                self.assertEqual(main(["--dry-run"]), 0)
                platform.assert_not_called()
                handler.assert_not_called()

        def test_main_live_once_restores_signal_and_clears(self):
            module = sys.modules[__name__]
            platform = FakePlatform()
            with (
                patch.object(module, "WindowsPlatform", return_value=platform),
                patch.object(signal, "signal", return_value=signal.SIG_DFL) as handler,
            ):
                self.assertEqual(main(["--once"]), 0)
            self.assertEqual(platform.calls, [True, False])
            self.assertEqual(handler.call_args.args, (signal.SIGTERM, signal.SIG_DFL))

        def test_main_reports_native_failure_without_traceback(self):
            module = sys.modules[__name__]
            with patch.object(module, "WindowsPlatform", side_effect=OSError("unsupported")):
                self.assertEqual(main([]), 1)

        def test_stop_raises_interrupt(self):
            with self.assertRaises(KeyboardInterrupt):
                stop(signal.SIGTERM, None)

    suite = unittest.TestSuite()
    for case in (RunnerTests, NativeBoundaryTests, CliTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    with patch.object(LOGGER, "disabled", True):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
