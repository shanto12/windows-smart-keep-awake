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

    def test_lock_after_auto(self):
        self.assertIsNone(keep_awake.parse_lock_after("auto"))
        self.assertEqual(keep_awake.parse_lock_after("900"), 900.0)


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
    def test_pulse_starts_at_lead_threshold(self):
        self.assertFalse(
            keep_awake.should_pulse(True, 569, 600, 30, 100, None, 180)
        )
        self.assertTrue(
            keep_awake.should_pulse(True, 570, 600, 30, 100, None, 180)
        )

    def test_pulse_respects_schedule_and_cooldown(self):
        self.assertFalse(
            keep_awake.should_pulse(False, 900, 600, 30, 100, None, 180)
        )
        self.assertFalse(
            keep_awake.should_pulse(True, 900, 600, 30, 200, 100, 180)
        )
        self.assertTrue(
            keep_awake.should_pulse(True, 900, 600, 30, 281, 100, 180)
        )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.now_value = dt.datetime(2026, 7, 30, 10, 0)
        self.monotonic_value = 100.0
        self.platform = FakePlatform(idle_seconds=570)
        settings = keep_awake.Settings(
            schedule=keep_awake.Schedule(dt.time(8), dt.time(18)),
            lock_after_seconds=600,
            wake_before_seconds=30,
            cooldown_seconds=180,
            input_mode="f24",
        )
        self.runner = keep_awake.KeepAwakeRunner(
            settings,
            self.platform,
            now=lambda: self.now_value,
            monotonic=lambda: self.monotonic_value,
        )

    def test_inside_window_enables_power_keep_awake_and_pulses(self):
        scheduled, idle = self.runner.step()
        self.assertTrue(scheduled)
        self.assertEqual(idle, 570)
        self.assertEqual(self.platform.keep_awake_calls, [True])
        self.assertEqual(self.platform.pulses, ["f24"])

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
            lock_after_seconds=600,
            input_mode="f24",
            dry_run=True,
        )
        self.runner.step()
        self.assertEqual(self.platform.keep_awake_calls, [])
        self.assertEqual(self.platform.pulses, [])


if __name__ == "__main__":
    unittest.main()
