"""Offline tests: every Windows call is faked, so these run on any OS.

    python -m unittest discover -s tests -v
"""

import datetime as dt
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import keep_awake as ka  # noqa: E402

ka.LOG.handlers[:] = [logging.NullHandler()]
ka.LOG.propagate = False

MONDAY = dt.date(2026, 10, 26)


def at(hour, minute=0, day=MONDAY):
    return dt.datetime.combine(day, dt.time(hour, minute))


class Clock:
    """Wall clock + monotonic clock that advance together on sleep()."""

    def __init__(self, start):
        self.wall = start
        self.mono = 0.0
        self.sleeps = []

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        assert seconds > 0, "runner must never busy-wait"
        self.sleeps.append(seconds)
        self.mono += seconds
        self.wall += dt.timedelta(seconds=seconds)


class FakePlatform:
    def __init__(self, clock, registers=("f24", "mouse"), desktop=True, timeouts=(None, None)):
        self.clock = clock
        self.registers = set(registers)
        self.desktop = desktop
        self.timeouts = timeouts
        self.last_input = clock.mono  # user just touched the PC
        self.tick = 0
        self.awake_calls = []
        self.sent = []

    def claim_single_instance(self):
        return True

    def set_awake(self, enabled):
        self.awake_calls.append(enabled)

    def lock_timeouts(self):
        return self.timeouts

    def desktop_ready(self):
        return self.desktop

    def user_holding_input(self):
        return False

    def input_state(self):
        return self.tick, self.clock.mono - self.last_input

    def send_activity(self, mode):
        self.sent.append((mode, self.clock.wall.time()))
        if mode in self.registers:
            self.tick += 1
            self.last_input = self.clock.mono
        return True


def run(start, seconds, **kwargs):
    clock = Clock(start)
    platform_kwargs = {k: kwargs.pop(k) for k in ("registers", "desktop", "timeouts")
                       if k in kwargs}
    platform = FakePlatform(clock, **platform_kwargs)
    runner = ka.Runner(platform, kwargs.pop("schedule", ka.Schedule()), duration=seconds,
                       now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep,
                       **kwargs)
    runner.run()
    return runner, platform, clock


class ParsingTests(unittest.TestCase):
    def test_parse_range_and_days(self):
        self.assertEqual(ka.parse_range("08:30-17:45"), (dt.time(8, 30), dt.time(17, 45)))
        self.assertEqual(ka.parse_range("22:00-24:00"), (dt.time(22), dt.time(0)))
        self.assertEqual(ka.parse_days("mon-fri"), frozenset(range(5)))
        self.assertEqual(ka.parse_days("mon,wed,fri"), frozenset({0, 2, 4}))
        self.assertEqual(ka.parse_days("fri-mon"), frozenset({4, 5, 6, 0}))
        for bad in ("8-17", "25:00-26:00", "08:00"):
            with self.assertRaises(ValueError):
                ka.parse_range(bad)
        with self.assertRaises(ValueError):
            ka.parse_days("funday")

    def test_cli_defaults(self):
        args = ka.build_parser().parse_args([])
        self.assertEqual(args.hours, (dt.time(8), dt.time(18)))
        self.assertIsNone(args.breaks)
        self.assertEqual(args.input_mode, "auto")


class ScheduleTests(unittest.TestCase):
    def test_phases(self):
        s = ka.Schedule()
        self.assertEqual(s.phase(at(7, 59))[0], ka.OFF)
        self.assertEqual(s.phase(at(8, 0))[0], ka.WORK)
        self.assertEqual(s.phase(at(11, 59))[0], ka.WORK)
        self.assertEqual(s.phase(at(12, 0))[0], ka.BREAK)
        self.assertEqual(s.phase(at(12, 59))[0], ka.BREAK)
        self.assertEqual(s.phase(at(13, 0))[0], ka.WORK)
        self.assertEqual(s.phase(at(18, 0))[0], ka.OFF)
        self.assertEqual(s.phase(at(10, day=dt.date(2026, 10, 31)))[0], ka.OFF)  # Saturday

    def test_overnight_and_holidays(self):
        night = ka.Schedule(hours=ka.parse_range("22:00-06:00"), breaks=())
        self.assertEqual(night.phase(at(23))[0], ka.WORK)
        self.assertEqual(night.phase(at(5))[0], ka.WORK)
        self.assertEqual(night.phase(at(12))[0], ka.OFF)
        self.assertEqual(ka.Schedule(holidays=frozenset({MONDAY})).phase(at(10)),
                         (ka.OFF, "holiday"))
        us = ka.Schedule(us_holidays=True)
        self.assertEqual(us.phase(at(10, day=dt.date(2026, 11, 26)))[0], ka.OFF)  # Thanksgiving

    def test_us_holiday_observance(self):
        h2026 = ka.us_federal_holidays(2026)
        self.assertIn(dt.date(2026, 7, 3), h2026)  # July 4 2026 is a Saturday
        self.assertIn(dt.date(2027, 12, 31), ka.us_federal_holidays(2027))  # Jan 1 2028 Sat
        self.assertEqual(len(h2026), 11)

    def test_effective_idle(self):
        self.assertEqual(ka.effective_idle_seconds(120, (None, None)), 120)
        self.assertEqual(ka.effective_idle_seconds(120, (60, 900)), 30)
        self.assertEqual(ka.effective_idle_seconds(120, (4, None)), ka.MIN_IDLE_SECONDS)


class RunnerTests(unittest.TestCase):
    def test_work_hours_keep_idle_below_threshold_with_f24(self):
        runner, platform, clock = run(at(9), 1800)
        self.assertEqual(platform.awake_calls, [True, False])
        self.assertTrue(platform.sent)
        self.assertEqual({mode for mode, _ in platform.sent}, {"f24"})
        # With the user away for 30 minutes, the idle time never reached Teams' 5 minutes.
        self.assertGreaterEqual(len(platform.sent), 1800 // 130)
        self.assertEqual(runner.pulses, len(platform.sent))

    def test_never_sends_while_user_active(self):
        clock = Clock(at(9))
        platform = FakePlatform(clock)
        sleep = clock.sleep

        def busy_sleep(seconds):
            sleep(seconds)
            platform.last_input = clock.mono  # user keeps typing

        runner = ka.Runner(platform, ka.Schedule(), duration=900, now=clock.now,
                           monotonic=clock.monotonic, sleep=busy_sleep)
        runner.run()
        self.assertEqual(platform.sent, [])

    def test_lunch_break_sends_nothing_but_stays_awake(self):
        _, platform, _ = run(at(12, 0), 3000)
        self.assertEqual(platform.sent, [])
        self.assertEqual(platform.awake_calls, [True, False])

    def test_break_transition(self):
        _, platform, _ = run(at(11, 50), 3600)
        times = [t for _, t in platform.sent]
        self.assertTrue(any(t < dt.time(12) for t in times))
        self.assertFalse(any(dt.time(12) <= t < dt.time(13) for t in times))
        self.assertEqual(platform.awake_calls, [True, False])  # stayed awake throughout

    def test_sleep_on_breaks(self):
        _, platform, _ = run(at(11, 59), 180, awake_on_breaks=False)
        self.assertEqual(platform.awake_calls, [True, False])
        self.assertTrue(all(t < dt.time(12) for _, t in platform.sent))

    def test_off_hours_release_and_poll_slowly(self):
        _, platform, clock = run(at(19), 600)
        self.assertEqual(platform.awake_calls, [])
        self.assertEqual(platform.sent, [])
        self.assertTrue(all(s >= ka.OFF_POLL_SECONDS for s in clock.sleeps[:-1]))

    def test_always_awake(self):
        _, platform, _ = run(at(19), 120, awake_off_hours=True)
        self.assertEqual(platform.awake_calls, [True, False])
        self.assertEqual(platform.sent, [])

    def test_falls_back_to_mouse_when_f24_not_registered(self):
        runner, platform, _ = run(at(9), 900, registers=("mouse",))
        modes = [m for m, _ in platform.sent]
        self.assertEqual(modes[0], "f24")
        self.assertEqual(set(modes[1:]), {"mouse"})
        self.assertEqual(runner.mode, "mouse")

    def test_backs_off_when_nothing_registers(self):
        _, platform, _ = run(at(9), 1200, registers=())
        # f24, mouse, then retry every 5 minutes; never a tight loop.
        self.assertLessEqual(len(platform.sent), 2 * (1 + 1200 // ka.RETRY_SECONDS) + 2)

    def test_locked_desktop_pauses_activity(self):
        runner, platform, _ = run(at(9), 900, desktop=False)
        self.assertEqual(platform.sent, [])
        self.assertEqual(platform.awake_calls, [True, False])

    def test_lock_timeout_lowers_threshold(self):
        runner, _, _ = run(at(9), 10, timeouts=(60.0, None))
        self.assertEqual(runner.threshold, 30)

    def test_input_none_keeps_awake_only(self):
        _, platform, _ = run(at(9), 900, input_mode="none")
        self.assertEqual(platform.sent, [])
        self.assertEqual(platform.awake_calls, [True, False])


class CliTests(unittest.TestCase):
    def tearDown(self):
        ka.LOG.handlers[:] = [logging.NullHandler()]

    def test_dry_run_and_status(self):
        self.assertEqual(ka.main(["--dry-run", "--duration", "0.05"]), 0)
        self.assertEqual(ka.main(["--dry-run", "--status"]), 0)
        self.assertEqual(ka.main(["--dry-run", "--no-breaks", "--us-holidays",
                                  "--holiday", "2026-12-24", "--duration", "0.05"]), 0)

    def test_bad_arguments_exit_2(self):
        with self.assertRaises(SystemExit) as ctx:
            ka.build_parser().parse_args(["--hours", "nope"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
