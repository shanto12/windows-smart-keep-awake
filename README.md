# Windows Smart Keep Awake

A single-file, dependency-free Python utility for Windows that keeps the machine awake during a local-time window while minimizing interference with normal work.

The default behavior is:

- active from **08:00 through 18:00 local time**;
- use Windows APIs to prevent system sleep during that window;
- read the visible Windows inactivity settings when possible;
- send one rare **F24 key down/up pair** only when the idle time is within 30 seconds of the detected timeout;
- check at most every 15 seconds and wait at the schedule boundary when outside the window;
- never inject input while the user is actively working, because the pulse is only considered after the idle threshold is reached.

## Run it

On the Windows laptop, with Python 3 installed:

```powershell
py keep_awake.py
```

Useful overrides:

```powershell
# Preview decisions without changing power state or sending input.
py keep_awake.py --dry-run --verbose

# Use a one-pixel relative mouse nudge instead of F24.
py keep_awake.py --input mouse

# Set an explicit 15-minute lock timeout and use a 45-second lead.
py keep_awake.py --lock-after 900 --wake-before 45

# Use a different local-time window.
py keep_awake.py --hours 07:30-19:00

# Only prevent sleep; do not synthesize keyboard or mouse input.
py keep_awake.py --input none
```

Press `Ctrl+C` to stop. The script clears its Windows power request on exit.

## Why it is conservative

The runner is intentionally a single loop with no third-party packages. It uses `GetLastInputInfo` for the real Windows idle duration, monotonic time for cooldowns, and local wall-clock time only for the daily schedule. It sleeps until the next relevant check, so it does not busy-wait.

F24 is used by default because it is a non-text virtual key and does not move the pointer. Applications can still choose to handle any synthetic input, so `--input mouse` and `--input none` are available. Use this only on a device you own or are authorized to manage, and only where it is consistent with your organization’s security policy. It does not override a centrally enforced lock policy.

`SetThreadExecutionState` prevents ordinary system sleep while the schedule is active. It does not guarantee that Windows will ignore a managed inactivity lock. Auto-detection covers visible screen-saver and machine inactivity settings; use `--lock-after SECONDS` when your environment has a hidden or centrally managed timeout.

## Tests

The decision logic and scheduling behavior are testable on any platform:

```powershell
python -m unittest discover -s tests -v
python -m py_compile keep_awake.py tests/test_keep_awake.py
```

The actual input and power calls are isolated behind `WindowsPlatform` and are only invoked on Windows.

## License

MIT. See [LICENSE](LICENSE).
