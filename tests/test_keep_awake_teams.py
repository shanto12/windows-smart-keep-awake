"""Offline tests for Windows sleep prevention and transparent console logging.

Run from the repository root:
    python -m unittest discover -s tests -v
    python -m unittest discover -s tests -p test_keep_awake_teams.py -v
"""

import argparse
import contextlib
import io
import logging
import signal
import sys
import time
import unittest
from unittest.mock import Mock, patch

import keep_awake_teams as awake


class RuntimeTestCase(unittest.TestCase):
    """Keep each test's logger configuration from affecting other test modules."""

    def setUp(self):
        self.logger_state = (
            list(awake.LOGGER.handlers),
            awake.LOGGER.level,
            awake.LOGGER.propagate,
            awake.LOGGER.disabled,
        )
        awake.LOGGER.handlers = []
        awake.LOGGER.disabled = True

    def tearDown(self):
        for handler in awake.LOGGER.handlers:
            handler.close()
        awake.LOGGER.handlers, level, awake.LOGGER.propagate, awake.LOGGER.disabled = (
            self.logger_state
        )
        awake.LOGGER.setLevel(level)


class FakePlatform:
    def __init__(self):
        self.calls = []

    def set_keep_awake(self, enabled):
        self.calls.append(enabled)


class RunnerTests(RuntimeTestCase):
    def test_default_holds_request_across_multiple_waits(self):
        platform = FakePlatform()
        waits = []

        def sleep(delay):
            self.assertEqual(platform.calls, [True])
            waits.append(delay)
            if len(waits) == 3:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            awake.KeepAwakeRunner(platform, sleep=sleep).run()
        self.assertEqual(platform.calls, [True, False])
        self.assertEqual(waits, [awake.WAIT_SECONDS] * 3)

    def test_duration_uses_elapsed_time_and_releases(self):
        platform = FakePlatform()
        elapsed = [100.0]
        waits = []

        def sleep(delay):
            self.assertEqual(platform.calls, [True])
            elapsed[0] += delay
            waits.append(delay)

        runner = awake.KeepAwakeRunner(
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
        awake.KeepAwakeRunner(
            platform, duration=0.25, monotonic=lambda: elapsed[0], sleep=sleep
        ).run()
        self.assertEqual(elapsed[0], 0.25)
        self.assertEqual(platform.calls, [True, False])

    def test_once_releases_without_waiting(self):
        platform = FakePlatform()
        sleep = Mock(side_effect=AssertionError("must not wait"))
        self.assertEqual(awake.KeepAwakeRunner(platform, sleep=sleep).run(once=True), 0)
        self.assertEqual(platform.calls, [True, False])
        sleep.assert_not_called()

    def test_dry_run_has_no_native_calls_or_waits(self):
        platform = Mock()
        sleep = Mock(side_effect=AssertionError("must not wait"))
        self.assertEqual(awake.KeepAwakeRunner(platform, dry_run=True, sleep=sleep).run(), 0)
        platform.set_keep_awake.assert_not_called()
        sleep.assert_not_called()

    def test_unexpected_error_still_releases_request(self):
        platform = FakePlatform()
        sleep = Mock(side_effect=RuntimeError("test failure"))
        with self.assertRaises(RuntimeError):
            awake.KeepAwakeRunner(platform, sleep=sleep).run()
        self.assertEqual(platform.calls, [True, False])

    def test_rejected_acquisition_is_not_reported_active(self):
        platform = Mock()
        platform.set_keep_awake.side_effect = OSError("request rejected")
        with self.assertRaises(OSError):
            awake.KeepAwakeRunner(platform).run(once=True)
        platform.set_keep_awake.assert_called_once_with(True)

    def test_cleanup_failure_is_reported(self):
        platform = Mock()
        platform.set_keep_awake.side_effect = [None, OSError("cleanup rejected")]
        with self.assertRaisesRegex(OSError, "cleanup rejected"):
            awake.KeepAwakeRunner(platform).run(once=True)
        self.assertEqual(platform.set_keep_awake.call_count, 2)


class NativeBoundaryTests(RuntimeTestCase):
    def test_only_system_request_flags_and_cleanup(self):
        platform = awake.WindowsPlatform.__new__(awake.WindowsPlatform)
        platform.kernel32 = Mock()
        platform.kernel32.SetThreadExecutionState.return_value = 1
        platform.set_keep_awake(True)
        platform.set_keep_awake(False)
        self.assertEqual(
            [call.args[0] for call in platform.kernel32.SetThreadExecutionState.call_args_list],
            [2147483649, 2147483648],
        )
        self.assertEqual(set(platform.kernel32._mock_children), {"SetThreadExecutionState"})

    def test_zero_native_return_is_an_error(self):
        platform = awake.WindowsPlatform.__new__(awake.WindowsPlatform)
        platform.kernel32 = Mock()
        platform.kernel32.SetThreadExecutionState.return_value = 0
        with self.assertRaises(OSError):
            platform.set_keep_awake(True)


class CliTests(RuntimeTestCase):
    def test_default_is_no_teams_and_no_duration(self):
        args = awake.build_parser().parse_args([])
        self.assertEqual(args.teams, "off")
        self.assertIsNone(args.duration)
        self.assertEqual(awake.build_parser().parse_args(["--teams", "off"]).teams, "off")

    def test_invalid_duration_values(self):
        for value in ("nan", "inf", "-1", "0", "bad"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                awake.positive_seconds(value)

    def test_former_graph_and_schedule_options_are_rejected(self):
        for args in (
            ["--teams", "graph"],
            ["--hours", "08:00-18:00"],
            ["--holiday", "2026-12-25"],
            ["--client-id", "example"],
            ["--presence-mode", "preferred"],
        ):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                awake.build_parser().parse_args(args)
            self.assertEqual(result.exception.code, 2)

    def test_tests_are_run_separately_from_runtime(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            awake.build_parser().parse_args(["--self-test"])
        self.assertEqual(result.exception.code, 2)

    def test_main_dry_run_does_not_load_windows_or_change_signal(self):
        module = awake
        with (
            patch.object(module, "WindowsPlatform") as platform,
            patch.object(signal, "signal") as handler,
        ):
            self.assertEqual(awake.main(["--dry-run"]), 0)
            platform.assert_not_called()
            handler.assert_not_called()

    def test_main_live_once_restores_signal_and_clears(self):
        module = awake
        platform = FakePlatform()
        with (
            patch.object(module, "WindowsPlatform", return_value=platform),
            patch.object(signal, "signal", return_value=signal.SIG_DFL) as handler,
        ):
            self.assertEqual(awake.main(["--once"]), 0)
        self.assertEqual(platform.calls, [True, False])
        self.assertEqual(handler.call_args.args, (signal.SIGTERM, signal.SIG_DFL))

    def test_main_reports_native_failure_without_traceback(self):
        module = awake
        with patch.object(module, "WindowsPlatform", side_effect=OSError("unsupported")):
            self.assertEqual(awake.main([]), 1)

    def test_stop_raises_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            awake.stop(signal.SIGTERM, None)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.saved = (
            list(awake.LOGGER.handlers),
            awake.LOGGER.level,
            awake.LOGGER.propagate,
            awake.LOGGER.disabled,
        )
        awake.LOGGER.handlers = []
        awake.LOGGER.disabled = False

    def tearDown(self):
        for handler in awake.LOGGER.handlers:
            handler.close()
        awake.LOGGER.handlers, level, awake.LOGGER.propagate, awake.LOGGER.disabled = self.saved
        awake.LOGGER.setLevel(level)

    def test_default_console_output_with_existing_root_configuration(self):
        module = awake
        console = io.StringIO()
        root = logging.getLogger()
        root_handler = logging.StreamHandler(io.StringIO())
        platform = FakePlatform()
        with (
            patch.object(root, "handlers", [root_handler]),
            patch.object(root, "level", logging.WARNING),
            patch.object(sys, "stdout", console),
            patch.object(module, "WindowsPlatform", return_value=platform),
            patch.object(signal, "signal", return_value=signal.SIG_DFL),
        ):
            self.assertEqual(awake.main(["--once"]), 0)
            self.assertEqual(root.handlers, [root_handler])
            self.assertEqual(root.level, logging.WARNING)
        lines = console.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Running:", lines[0])
        self.assertIn("Stopped: Windows sleep-prevention request released", lines[1])
        self.assertEqual(platform.calls, [True, False])

    def test_repeated_setup_uses_current_stdout_without_duplicates(self):
        first, second = (io.StringIO(), io.StringIO())
        external = logging.NullHandler()
        awake.LOGGER.addHandler(external)
        with patch.object(sys, "stdout", first):
            awake.configure_logging()
            awake.LOGGER.info("first run")
        with patch.object(sys, "stdout", second):
            awake.configure_logging()
            awake.LOGGER.info("second run")
        self.assertEqual(first.getvalue().count("first run"), 1)
        self.assertNotIn("second run", first.getvalue())
        self.assertEqual(second.getvalue().count("second run"), 1)
        self.assertIn(external, awake.LOGGER.handlers)
        self.assertEqual(
            sum((bool(getattr(h, "_keep_awake_console", False)) for h in awake.LOGGER.handlers)), 1
        )

    def test_verbose_adds_native_details_only_when_requested(self):
        console = io.StringIO()
        platform = awake.WindowsPlatform.__new__(awake.WindowsPlatform)
        platform.kernel32 = Mock()
        platform.kernel32.SetThreadExecutionState.return_value = 1
        with patch.object(sys, "stdout", console):
            awake.configure_logging()
            platform.set_keep_awake(True)
            self.assertEqual(console.getvalue(), "")
            awake.configure_logging(verbose=True)
            platform.set_keep_awake(False)
        self.assertIn("DEBUG SetThreadExecutionState flags=0x80000000", console.getvalue())

    def test_heartbeat_every_five_minutes_without_native_refresh(self):
        platform = FakePlatform()
        clock = [0.0]

        def sleep(delay):
            clock[0] += delay
            if clock[0] >= 630:
                raise KeyboardInterrupt

        with (
            self.assertLogs(awake.LOGGER, level="DEBUG") as logs,
            self.assertRaises(KeyboardInterrupt),
        ):
            awake.KeepAwakeRunner(platform, monotonic=lambda: clock[0], sleep=sleep).run()
        heartbeats = [
            record.getMessage() for record in logs.records if "Still running" in record.getMessage()
        ]
        self.assertEqual(len(heartbeats), 2)
        self.assertIn("300s elapsed", heartbeats[0])
        self.assertIn("600s elapsed", heartbeats[1])
        self.assertEqual(platform.calls, [True, False])
        self.assertEqual(sum((record.levelno == logging.DEBUG for record in logs.records)), 1)

    def test_late_wake_emits_one_heartbeat_without_catch_up(self):
        platform = FakePlatform()
        clock = [0.0]
        waits = []

        def sleep(delay):
            waits.append(delay)
            if len(waits) == 1:
                clock[0] = 1199
            elif len(waits) == 2:
                clock[0] += delay
            else:
                raise KeyboardInterrupt

        with (
            self.assertLogs(awake.LOGGER, level="INFO") as logs,
            self.assertRaises(KeyboardInterrupt),
        ):
            awake.KeepAwakeRunner(platform, monotonic=lambda: clock[0], sleep=sleep).run()
        heartbeats = [record for record in logs.records if "Still running" in record.getMessage()]
        self.assertEqual(len(heartbeats), 1)
        self.assertEqual(waits, [awake.WAIT_SECONDS] * 3)
        self.assertEqual(platform.calls, [True, False])

    def test_duration_expiry_precedes_heartbeat(self):
        platform = FakePlatform()
        clock = [0.0]

        def sleep(delay):
            clock[0] += delay

        with self.assertLogs(awake.LOGGER, level="INFO") as logs:
            awake.KeepAwakeRunner(
                platform, duration=900, monotonic=lambda: clock[0], sleep=sleep
            ).run()
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(len(messages), 4)
        self.assertEqual(sum(("Still running" in message for message in messages)), 2)
        self.assertEqual(messages[-1], "Stopped: Windows sleep-prevention request released")

    def test_native_failures_do_not_log_false_success(self):
        for calls, forbidden in (
            ([OSError("acquire failed")], ("Running:", "request released")),
            ([None, OSError("release failed")], ("request released",)),
        ):
            console = io.StringIO()
            platform = Mock()
            platform.set_keep_awake.side_effect = calls
            with patch.object(sys, "stdout", console):
                awake.configure_logging()
                with self.assertRaises(OSError):
                    awake.KeepAwakeRunner(platform).run(once=True)
            for text in forbidden:
                self.assertNotIn(text, console.getvalue())

    def test_interruption_logs_one_clean_shutdown(self):
        module = awake
        console = io.StringIO()
        platform = FakePlatform()
        with (
            patch.object(sys, "stdout", console),
            patch.object(module, "WindowsPlatform", return_value=platform),
            patch.object(signal, "signal", return_value=signal.SIG_DFL),
            patch.object(time, "sleep", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(awake.main([]), 130)
        lines = console.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Running:", lines[0])
        self.assertTrue(
            lines[-1].endswith("INFO Stopped: Windows sleep-prevention request released")
        )
        self.assertEqual(platform.calls, [True, False])


if __name__ == "__main__":
    unittest.main()
