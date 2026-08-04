# Windows Smart Keep Awake

A single-file, dependency-free Python utility for Windows that keeps the machine awake — and Microsoft Teams showing **Available** — during a local-time window, while minimizing interference with normal work.

The default behavior is:

- active from **08:00 through 18:00 local time**;
- prevent system sleep and keep the display on with `SetThreadExecutionState` during that window;
- send one rare **F24 key down/up pair** whenever the machine has been idle for **4 minutes**;
- print a startup banner, a **dot every 5 minutes** so you can see it is still running, and a **`*`** each time a pulse is sent;
- never inject input while you are actively working — a pulse is only considered once the idle threshold is reached.

## Why this keeps Teams green

Teams marks you *Away* after about 5 minutes without keyboard or mouse input. It reads the same Windows last-input timer that controls screen lock and idle sleep. The synthetic F24 pulse resets that timer, so pulsing at 4 minutes of idle keeps Teams on *Available*, keeps the screen unlocked, and keeps the machine awake — one mechanism covers all three. F24 is a non-text key that regular applications ignore, so it does not type characters or move the pointer.

## Run it

On the Windows laptop, with Python 3 installed:

```powershell
py keep_awake.py
```

You should immediately see something like:

```
windows-smart-keep-awake v2.0.0
active window 08:00-18:00 local time; f24 pulse when idle >= 240s
output: '.' every 300s means still running, '*' means pulse sent
press Ctrl+C to stop
[2026-08-04 09:12:03] inside active window 08:00-18:00: keeping Windows awake and Teams available
..*..*.
```

Useful overrides:

```powershell
# Preview decisions without changing power state or sending input.
py keep_awake.py --dry-run

# Use a one-pixel relative mouse nudge instead of F24.
py keep_awake.py --input mouse

# Pulse earlier (every 3 minutes of idle) and print a dot every minute.
py keep_awake.py --max-idle 180 --heartbeat 60

# Use a different local-time window (overnight windows work too).
py keep_awake.py --hours 07:30-19:00
```

Press `Ctrl+C` to stop. The script clears its Windows power request on exit.

## Why it is conservative

The runner is intentionally a single loop with no third-party packages. It uses `GetLastInputInfo` for the real Windows idle duration, monotonic time for pacing, and local wall-clock time only for the daily schedule. It sleeps between checks (30 seconds by default inside the window), so it does not busy-wait, and outside the window it waits quietly for the next start time while still printing its heartbeat.

Use this only on a device you own or are authorized to manage, and only where it is consistent with your organization's security policy. It does not override a centrally enforced lock policy.

## Tests

The decision logic and scheduling behavior are testable on any platform:

```powershell
python -m unittest discover -s tests -v
python -m py_compile keep_awake.py tests/test_keep_awake.py
```

The actual input and power calls are isolated behind `WindowsPlatform` and are only invoked on Windows.

## License

MIT. See [LICENSE](LICENSE).
