"""Offline tests for Windows power requests and guarded idle prevention.

Run from the repository root: python -m unittest discover -s tests -v
All Windows APIs are replaced with fakes; these tests never synthesize input.
"""

import ctypes
import unittest
from unittest.mock import Mock, patch

import keep_awake_teams as awake


class RuntimeTestCase(unittest.TestCase):
    """Isolate logger configuration from the original script's test module."""

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


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.waits = []
        self.after_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        if delay <= 0:
            raise AssertionError("the runner must not busy-wait")
        self.waits.append(delay)
        self.now += delay
        if self.after_sleep is not None:
            self.after_sleep(self)


class FakePlatform:
    def __init__(self, clock, initial_idle=0.0):
        self.clock = clock
        self.power_calls = []
        self.calls = []
        self.last_input_at = clock.now - initial_idle
        self.tick = 1000
        self.desktop = True
        self.keys = False
        self.accept_pulse = True
        self.reset_on_pulse = True
        self.pulse_times = []
        self.settings = awake.IdleSettings()

    def read_idle_settings(self):
        self.calls.append("settings")
        return self.settings

    def set_keep_awake(self, enabled):
        self.power_calls.append(enabled)

    def get_input_state(self):
        self.calls.append("input")
        return awake.InputState(self.tick, self.clock.now - self.last_input_at)

    def desktop_available(self):
        self.calls.append("desktop")
        return self.desktop

    def held_keys(self):
        self.calls.append("keys")
        return self.keys

    def send_idle_pulse(self, minimum_idle_seconds=0.0):
        self.calls.append("pulse")
        self.pulse_times.append(self.clock.now)
        if self.accept_pulse and self.reset_on_pulse:
            self.user_input()
        return self.accept_pulse

    def user_input(self):
        self.tick += 1
        self.last_input_at = self.clock.now


def make_runner(platform=None, clock=None, **kwargs):
    clock = clock or FakeClock()
    platform = platform or FakePlatform(clock)
    return awake.KeepAwakeRunner(
        platform=platform,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        **kwargs,
    )


class PowerLifecycleTests(RuntimeTestCase):
    def test_once_acquires_and_releases_without_input_or_waits(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        self.assertEqual(make_runner(platform, clock).run(once=True), 0)
        self.assertEqual(platform.power_calls, [True, False])
        self.assertEqual(platform.calls, [])
        self.assertEqual(clock.waits, [])

    def test_dry_run_has_no_native_calls_or_waits(self):
        platform = Mock()
        clock = FakeClock()
        self.assertEqual(make_runner(platform, clock, dry_run=True).run(), 0)
        self.assertEqual(platform.mock_calls, [])
        self.assertEqual(clock.waits, [])

    def test_allow_lock_never_reads_idle_or_sends_input(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        self.assertEqual(make_runner(platform, clock, duration=65, allow_lock=True).run(), 0)
        self.assertEqual(clock.now, 65)
        self.assertEqual(platform.power_calls, [True, False])
        self.assertEqual(platform.calls, [])

    def test_fractional_duration_is_not_rounded_up(self):
        clock = FakeClock(now=100)
        platform = FakePlatform(clock)
        make_runner(platform, clock, duration=0.25, allow_lock=True).run()
        self.assertEqual(clock.now, 100.25)
        self.assertEqual(platform.power_calls, [True, False])

    def test_interrupt_always_releases_power_request(self):
        clock = FakeClock()
        platform = FakePlatform(clock)

        def interrupt(_):
            raise KeyboardInterrupt

        clock.after_sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            make_runner(platform, clock, allow_lock=True).run()
        self.assertEqual(platform.power_calls, [True, False])

    def test_unexpected_loop_error_always_releases_power_request(self):
        clock = FakeClock()
        platform = FakePlatform(clock)

        def fail(_):
            raise RuntimeError("unexpected loop failure")

        clock.after_sleep = fail
        with self.assertRaisesRegex(RuntimeError, "unexpected loop failure"):
            make_runner(platform, clock, allow_lock=True).run()
        self.assertEqual(platform.power_calls, [True, False])

    def test_failed_acquisition_is_not_released_as_if_acquired(self):
        platform = Mock()
        platform.set_keep_awake.side_effect = OSError("power request denied")
        with self.assertRaisesRegex(OSError, "power request denied"):
            make_runner(platform).run(once=True)
        platform.set_keep_awake.assert_called_once_with(True)

    def test_release_failure_is_not_swallowed(self):
        platform = Mock()
        platform.set_keep_awake.side_effect = [None, OSError("release failed")]
        with self.assertRaisesRegex(OSError, "release failed"):
            make_runner(platform).run(once=True)
        self.assertEqual(platform.set_keep_awake.call_count, 2)


class IdleGuardTests(RuntimeTestCase):
    def test_active_user_does_not_receive_synthetic_input(self):
        clock = FakeClock()
        platform = FakePlatform(clock)
        clock.after_sleep = lambda _: platform.user_input()
        make_runner(platform, clock, duration=120).run()
        self.assertEqual(platform.pulse_times, [])
        self.assertEqual(platform.power_calls, [True, False])

    def test_idle_pulses_wait_for_threshold_and_reset_observation(self):
        clock = FakeClock()
        platform = FakePlatform(clock)
        make_runner(platform, clock, duration=200, idle_seconds=45).run()
        self.assertGreaterEqual(len(platform.pulse_times), 3)
        self.assertLessEqual(len(platform.pulse_times), 4)
        self.assertGreaterEqual(platform.pulse_times[0], 45)
        self.assertTrue(
            all(b - a >= 45 for a, b in zip(platform.pulse_times, platform.pulse_times[1:]))
        )
        self.assertEqual(platform.power_calls, [True, False])

    def test_locked_or_secure_desktop_pauses_input(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.desktop = False
        make_runner(platform, clock, duration=60).run()
        self.assertEqual(platform.pulse_times, [])
        self.assertNotIn("input", platform.calls)
        self.assertEqual(platform.power_calls, [True, False])

    def test_input_resumes_only_after_normal_desktop_returns(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.desktop = False

        def unlock(_):
            if clock.now >= 20:
                platform.desktop = True

        clock.after_sleep = unlock
        make_runner(platform, clock, duration=60).run()
        self.assertTrue(platform.pulse_times)
        self.assertGreaterEqual(platform.pulse_times[0], 20)

    def test_held_modifier_or_mouse_button_suppresses_input(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.keys = True
        make_runner(platform, clock, duration=60).run()
        self.assertEqual(platform.pulse_times, [])
        self.assertEqual(platform.power_calls, [True, False])

    def test_guard_retries_after_held_keys_are_released(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.keys = True

        def release_keys(_):
            if clock.now >= 20:
                platform.keys = False

        clock.after_sleep = release_keys
        make_runner(platform, clock, duration=60).run()
        self.assertTrue(platform.pulse_times)
        self.assertGreaterEqual(platform.pulse_times[0], 20)

    def test_accepted_input_without_idle_reset_aborts_and_releases(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.reset_on_pulse = False
        with self.assertRaises(OSError):
            make_runner(platform, clock, duration=60).run()
        self.assertEqual(len(platform.pulse_times), 1)
        self.assertEqual(platform.power_calls, [True, False])

    def test_changed_tick_alone_does_not_claim_idle_reset(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.reset_on_pulse = False

        def misleading_tick(_):
            platform.tick += 1

        clock.after_sleep = misleading_tick
        with self.assertRaises(OSError):
            make_runner(platform, clock, duration=60).run()
        self.assertEqual(len(platform.pulse_times), 1)
        self.assertEqual(platform.power_calls, [True, False])

    def test_input_read_failure_releases_power_request(self):
        clock = FakeClock()
        platform = FakePlatform(clock)
        platform.get_input_state = Mock(side_effect=OSError("idle read failed"))
        with self.assertRaisesRegex(OSError, "idle read failed"):
            make_runner(platform, clock, duration=60).run()
        self.assertEqual(platform.pulse_times, [])
        self.assertEqual(platform.power_calls, [True, False])

    def test_explicit_synthetic_reset_restriction_stops_before_power_request(self):
        clock = FakeClock()
        platform = FakePlatform(clock)
        platform.settings = awake.IdleSettings(blocks_synthetic_resets=True)
        with self.assertRaisesRegex(OSError, "blocks simulated-input"):
            make_runner(platform, clock, duration=60).run()
        self.assertEqual(platform.power_calls, [])
        self.assertEqual(platform.pulse_times, [])

    def test_delayed_input_processing_is_verified_within_grace_period(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.reset_on_pulse = False

        def eventually_reset(_):
            if clock.now >= 1.5 and platform.tick == 1000:
                platform.user_input()

        clock.after_sleep = eventually_reset
        self.assertEqual(make_runner(platform, clock, duration=10).run(), 0)
        self.assertEqual(len(platform.pulse_times), 1)
        self.assertEqual(platform.power_calls, [True, False])

    def test_lock_during_verification_pauses_instead_of_injecting_again(self):
        clock = FakeClock()
        platform = FakePlatform(clock, initial_idle=1000)
        platform.reset_on_pulse = False
        clock.after_sleep = lambda _: setattr(platform, "desktop", False)
        self.assertEqual(make_runner(platform, clock, duration=10).run(), 0)
        self.assertEqual(len(platform.pulse_times), 1)
        self.assertEqual(platform.power_calls, [True, False])


class TimeoutSelectionTests(RuntimeTestCase):
    def test_unknown_timeouts_keep_requested_idle_interval(self):
        self.assertEqual(awake.choose_idle_seconds(45, awake.IdleSettings()), 45)

    def test_shortest_known_timeout_has_half_timeout_margin(self):
        settings = awake.IdleSettings(screen_saver_seconds=120, machine_lock_seconds=30)
        self.assertEqual(awake.choose_idle_seconds(45, settings), 15)

    def test_user_requested_shorter_interval_is_preserved(self):
        settings = awake.IdleSettings(screen_saver_seconds=120, machine_lock_seconds=300)
        self.assertEqual(awake.choose_idle_seconds(20, settings), 20)

    def test_zero_disabled_timeouts_are_ignored(self):
        settings = awake.IdleSettings(screen_saver_seconds=0, machine_lock_seconds=0)
        self.assertEqual(awake.choose_idle_seconds(45, settings), 45)


class NativeBoundaryTests(RuntimeTestCase):
    def platform(self):
        platform = awake.WindowsPlatform.__new__(awake.WindowsPlatform)
        platform.kernel32 = Mock()
        platform.user32 = Mock()
        return platform

    def pulse_platform(self):
        platform = self.platform()
        platform.desktop_available = Mock(return_value=True)
        platform.held_keys = Mock(return_value=False)
        platform.get_input_state = Mock(return_value=awake.InputState(1000, 1000))
        return platform

    def test_windows_abi_sizes_and_native_signatures(self):
        wide = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(awake.DWORD), 4)
        self.assertEqual(ctypes.sizeof(awake.LONG), 4)
        self.assertEqual(ctypes.sizeof(awake.BOOL), 4)
        self.assertEqual(ctypes.sizeof(awake.LASTINPUTINFO), 8)
        self.assertEqual(ctypes.sizeof(awake.KEYBDINPUT), 24 if wide else 16)
        self.assertEqual(ctypes.sizeof(awake.MOUSEINPUT), 32 if wide else 24)
        self.assertEqual(ctypes.sizeof(awake.INPUT), 40 if wide else 28)
        kernel32, user32 = Mock(), Mock()
        with (
            patch.object(awake.os, "name", "nt"),
            patch.object(awake.ctypes, "WinDLL", create=True, side_effect=[kernel32, user32]),
        ):
            awake.WindowsPlatform()
        self.assertEqual(user32.SendInput.argtypes[1], ctypes.POINTER(awake.INPUT))
        self.assertEqual(user32.SendInput.argtypes[2], ctypes.c_int)
        self.assertEqual(user32.OpenInputDesktop.restype, ctypes.c_void_p)
        self.assertEqual(user32.CloseDesktop.argtypes, [ctypes.c_void_p])
        self.assertEqual(user32.GetAsyncKeyState.restype, ctypes.c_int16)
        self.assertEqual(kernel32.SetThreadExecutionState.restype, awake.DWORD)

    def test_system_and_display_request_flags_and_rejection(self):
        platform = self.platform()
        platform.kernel32.SetThreadExecutionState.return_value = 1
        platform.set_keep_awake(True)
        platform.set_keep_awake(False)
        self.assertEqual(
            [call.args[0] for call in platform.kernel32.SetThreadExecutionState.call_args_list],
            [0x80000003, 0x80000000],
        )
        platform.kernel32.SetThreadExecutionState.return_value = 0
        with self.assertRaises(OSError):
            platform.set_keep_awake(True)

    def test_idle_clock_rollover_and_read_failure(self):
        platform = self.platform()

        def last_input(pointer):
            state = ctypes.cast(pointer, ctypes.POINTER(awake.LASTINPUTINFO)).contents
            self.assertEqual(state.cbSize, 8)
            state.dwTime = 0xFFFFFF00
            return 1

        platform.user32.GetLastInputInfo.side_effect = last_input
        platform.kernel32.GetTickCount.return_value = 0x100
        state = platform.get_input_state()
        self.assertEqual(state.tick, 0xFFFFFF00)
        self.assertAlmostEqual(state.idle_seconds, 0.512)
        platform.user32.GetLastInputInfo.side_effect = None
        platform.user32.GetLastInputInfo.return_value = 0
        with self.assertRaises(OSError):
            platform.get_input_state()

    def test_idle_settings_use_read_queries_and_respect_disabled_screen_saver(self):
        platform = self.platform()
        settings = {
            awake.SPI_GETSCREENSAVEACTIVE: 1,
            awake.SPI_GETSCREENSAVETIMEOUT: 60,
            awake.SPI_GETBLOCKSENDINPUTRESETS: 1,
        }

        def read_setting(action, value, pointer, flags):
            self.assertEqual((value, flags), (0, 0))
            ctypes.cast(pointer, ctypes.POINTER(awake.DWORD)).contents.value = settings[action]
            return 1

        platform.user32.SystemParametersInfoW.side_effect = read_setting
        with patch.object(awake, "machine_lock_seconds", return_value=30):
            self.assertEqual(platform.read_idle_settings(), awake.IdleSettings(60, 30, True))
            settings[awake.SPI_GETSCREENSAVEACTIVE] = 0
            platform.user32.SystemParametersInfoW.reset_mock()
            self.assertIsNone(platform.read_idle_settings().screen_saver_seconds)
        actions = [c.args[0] for c in platform.user32.SystemParametersInfoW.call_args_list]
        self.assertNotIn(awake.SPI_GETSCREENSAVETIMEOUT, actions)
        platform.user32.SystemParametersInfoW.side_effect = None
        platform.user32.SystemParametersInfoW.return_value = 0
        with patch.object(awake, "machine_lock_seconds", return_value=None):
            self.assertEqual(platform.read_idle_settings(), awake.IdleSettings())

    def test_desktop_requires_default_name_and_input_and_always_closes_handle(self):
        for name, receiving, fail_action, expected in (
            ("Default", True, None, True),
            ("Winlogon", True, None, False),
            ("Default", False, None, False),
            ("Default", True, awake.UOI_NAME, False),
            ("Default", True, awake.UOI_IO, False),
        ):
            with self.subTest(name=name, receiving=receiving, fail_action=fail_action):
                platform = self.platform()
                platform.user32.OpenInputDesktop.return_value = 123

                def describe(desktop, action, pointer, size, needed):
                    self.assertEqual(desktop, 123)
                    if action == fail_action:
                        return 0
                    if action == awake.UOI_NAME:
                        pointer.value = name
                    else:
                        ctypes.cast(pointer, ctypes.POINTER(awake.BOOL)).contents.value = receiving
                    return 1

                platform.user32.GetUserObjectInformationW.side_effect = describe
                self.assertEqual(platform.desktop_available(), expected)
                platform.user32.CloseDesktop.assert_called_once_with(123)
        platform = self.platform()
        platform.user32.OpenInputDesktop.return_value = 0
        self.assertFalse(platform.desktop_available())
        platform.user32.CloseDesktop.assert_not_called()

    def test_held_key_guard_uses_current_state_bit_not_prior_press_bit(self):
        platform = self.platform()
        platform.user32.GetAsyncKeyState.return_value = 1
        self.assertFalse(platform.held_keys())
        for key in awake.GUARD_KEYS:
            with self.subTest(key=key):
                platform.user32.GetAsyncKeyState.side_effect = lambda queried, held=key: (
                    -32768 if queried == held else 0
                )
                self.assertTrue(platform.held_keys())

    def test_pulse_is_one_f24_down_up_pair_with_correct_native_size(self):
        platform = self.pulse_platform()

        def send(count, events, size):
            self.assertEqual((count, size), (2, ctypes.sizeof(awake.INPUT)))
            self.assertEqual([event.type for event in events], [1, 1])
            self.assertEqual([event.ki.wVk for event in events], [awake.VK_F24, awake.VK_F24])
            self.assertEqual([event.ki.dwFlags for event in events], [0, awake.KEYEVENTF_KEYUP])
            self.assertEqual([event.ki.wScan for event in events], [0, 0])
            return 2

        platform.user32.SendInput.side_effect = send
        self.assertTrue(platform.send_idle_pulse())
        platform.user32.SendInput.assert_called_once()

    def test_last_moment_desktop_or_key_change_prevents_sendinput(self):
        for desktop, held in ((False, False), (True, True)):
            with self.subTest(desktop=desktop, held=held):
                platform = self.pulse_platform()
                platform.desktop_available.return_value = desktop
                platform.held_keys.return_value = held
                self.assertFalse(platform.send_idle_pulse())
                platform.user32.SendInput.assert_not_called()
        platform = self.pulse_platform()
        platform.get_input_state.return_value = awake.InputState(1001, 0)
        self.assertFalse(platform.send_idle_pulse(45))
        platform.user32.SendInput.assert_not_called()

    def test_zero_inserted_events_are_reported_without_extra_input(self):
        platform = self.pulse_platform()
        platform.user32.SendInput.return_value = 0
        with self.assertRaisesRegex(OSError, "accepted 0/2 events"):
            platform.send_idle_pulse()
        platform.user32.SendInput.assert_called_once()

    def test_partial_insert_attempts_only_f24_release_before_reporting_failure(self):
        platform = self.pulse_platform()
        platform.user32.SendInput.side_effect = [1, 1]
        with self.assertRaisesRegex(OSError, "accepted 1/2 events"):
            platform.send_idle_pulse()
        calls = platform.user32.SendInput.call_args_list
        self.assertEqual(len(calls), 2)
        count, events, size = calls[1].args
        self.assertEqual((count, size), (1, ctypes.sizeof(awake.INPUT)))
        self.assertEqual(events[0].ki.wVk, awake.VK_F24)
        self.assertEqual(events[0].ki.dwFlags, awake.KEYEVENTF_KEYUP)

    def test_partial_insert_release_failure_is_explicit(self):
        platform = self.pulse_platform()
        platform.user32.SendInput.side_effect = [1, 0]
        with self.assertRaisesRegex(OSError, "F24 release also failed"):
            platform.send_idle_pulse()
        self.assertEqual(platform.user32.SendInput.call_count, 2)
