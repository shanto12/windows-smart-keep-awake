import datetime as dt
import unittest

import keep_awake


class FakePlatform:
    def __init__(self, idle_seconds=0.0):
        self.idle_seconds = idle_seconds
        self.keep_awake_calls = []
        self.pulses = []

    def get_idle_seconds(self):
        return self.idle_seconds

    def set_keep_awake(self, enabled):
        self.keep_awake_calls.append(enabled)

    def pulse(self, mode):
        self.pulses.append(mode)


class ParseTests(unittest.TestCase):
    def test_clock_and_hours(self):
        self.assertEqual(keep_awake.parse_clock("08:05"), dt.time(8, 5))
        schedule = keep_awake.parse_hours("22:00-06:00")
        self.assertEqual(schedule.start, dt.time(22, 0))
        self.assertEqual(schedule.end, dt.time(6, 0))

    def test_parse_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            keep_awake.parse_clock("25:00")
        with self.assertRaises(ValueError):
            keep_awake.parse_hours("08:00")
        with self.assertRaises(ValueError):
            keep_awake.parse_seconds("0")


class ScheduleTests(unittest.TestCase):
    def test_regular_window_is_start_inclusive_and_end_exclusive(self):
        schedule = keep_awake.Schedule(dt.time(8), dt.time(18))
        self.assertTrue(schedule.contains(dt.datetime(2026, 7, 30, 8, 0)))
        self.assertTrue(schedule.contains(dt.datetime(2026, 7, 30, 17, 59)))
        self.assertFalse(schedule.contains(dt.datetime(2026, 7, 30, 18, 0)))

    def test_overnight_window(self):
        schedule = keep_awake.Schedule(dt.time(22), dt.time(6))
        self.assertTrue(schedule.contains(dt.datetime(2026, 7, 30, 23)))
        self.assertTrue(schedule.contains(dt.datetime(2026, 7, 31, 5, 59)))
        self.assertFalse(schedule.contains(dt.datetime(2026, 7, 30, 12)))

    def test_same_start_and_end_means_all_day(self):
        schedule = keep_awake.Schedule(dt.time(0), dt.time(0))
        self.assertTrue(schedule.contains(dt.datetime(2026, 7, 30, 23, 59)))

    def test_seconds_until_start(self):
        schedule = keep_awake.Schedule(dt.time(8), dt.time(18))
        now = dt.datetime(2026, 7, 30, 7, 30)
        self.assertEqual(schedule.seconds_until_start(now), 1800.0)


class DecisionTests(unittest.TestCase):
    def test_pulse_starts_at_max_idle(self):
        self.assertFalse(keep_awake.should_pulse(True, 239, 240, 100, None))
        self.assertTrue(keep_awake.should_pulse(True, 240, 240, 100, None))

    def test_pulse_respects_schedule(self):
        self.assertFalse(keep_awake.should_pulse(False, 900, 240, 100, None))

    def test_pulse_rate_is_limited_when_idle_never_resets(self):
        self.assertFalse(keep_awake.should_pulse(True, 900, 240, 300, 100))
        self.assertTrue(keep_awake.should_pulse(True, 900, 240, 341, 100))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.now_value = dt.datetime(2026, 7, 30, 10, 0)
        self.monotonic_value = 100.0
        self.platform = FakePlatform(idle_seconds=240)
        self.output = []
        settings = keep_awake.Settings(
            schedule=keep_awake.Schedule(dt.time(8), dt.time(18)),
            max_idle_seconds=240,
            heartbeat_seconds=300,
            input_mode="f24",
        )
        self.runner = keep_awake.KeepAwakeRunner(
            settings,
            self.platform,
            now=lambda: self.now_value,
            monotonic=lambda: self.monotonic_value,
            echo=self.output.append,
        )

    def test_inside_window_enables_power_keep_awake_and_pulses(self):
        scheduled, idle = self.runner.step()
        self.assertTrue(scheduled)
        self.assertEqual(idle, 240)
        self.assertEqual(self.platform.keep_awake_calls, [True])
        self.assertEqual(self.platform.pulses, ["f24"])
        self.assertIn("*", self.output)

    def test_below_max_idle_does_not_pulse(self):
        self.platform.idle_seconds = 239
        self.runner.step()
        self.assertEqual(self.platform.pulses, [])

    def test_outside_window_disables_power_keep_awake(self):
        self.runner.step()
        self.now_value = dt.datetime(2026, 7, 30, 18, 0)
        scheduled, idle = self.runner.step()
        self.assertFalse(scheduled)
        self.assertIsNone(idle)
        self.assertEqual(self.platform.keep_awake_calls, [True, False])

    def test_dry_run_does_not_touch_platform(self):
        self.runner.settings = keep_awake.Settings(
            schedule=self.runner.settings.schedule,
            max_idle_seconds=240,
            input_mode="f24",
            dry_run=True,
        )
        self.runner.step()
        self.assertEqual(self.platform.keep_awake_calls, [])
        self.assertEqual(self.platform.pulses, [])

    def test_run_emits_heartbeat_dot(self):
        self.platform.idle_seconds = 0
        sleeps = []

        def fake_sleep(delay):
            sleeps.append(delay)
            self.monotonic_value += 400
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        self.runner.sleep = fake_sleep
        with self.assertRaises(KeyboardInterrupt):
            self.runner.run()
        self.assertIn(".", self.output)
        self.assertEqual(self.platform.keep_awake_calls, [True, False])


if __name__ == "__main__":
    unittest.main()
