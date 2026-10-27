# Windows Smart Keep Awake

One small Python file, using only the standard library. It keeps a Windows laptop or VM awake during work hours and keeps Microsoft Teams **Available** while you're away from the keyboard. During lunch it lets Teams go **Away** on its own, and it stays out of your way the rest of the time.

| Time (default)                 | PC sleep / display off | Activity input  | Teams status                         |
|--------------------------------|------------------------|-----------------|--------------------------------------|
| Mon–Fri 08:00–12:00, 13:00–18:00 | blocked                | when idle ≥ 2 min | Available                            |
| Lunch 12:00–13:00              | blocked                | **none**        | goes Away naturally (~5 min idle)    |
| Evenings, weekends, holidays   | normal Windows settings | none            | whatever Teams decides               |

## Quick start (any Windows 10/11 laptop or VM)

1. Install Python 3.9+ if `py --version` doesn't work: `winget install Python.Python.3.12`, or use [python.org](https://www.python.org/downloads/windows/).
2. Download this repo (green **Code** button → *Download ZIP*) and unzip it anywhere.
3. Double-click **`KeepAwake.cmd`**. Close the window or press `Ctrl+C` to stop.

**Start automatically at sign-in:** press `Win+R`, type `shell:startup`, and put a *shortcut* to `KeepAwake.cmd` in that folder. To minimise the window, open the shortcut's *Properties* and set *Run* to *Minimized*. Only one copy ever runs at a time.

To check what it sees right now, without changing anything:

```powershell
py keep_awake.py --status
```

## Options

```powershell
py keep_awake.py --hours 07:30-17:00 --break 12:30-13:30   # custom day
py keep_awake.py --break 10:30-10:45 --break 12:00-13:00   # several breaks
py keep_awake.py --no-breaks                                # Available all day
py keep_awake.py --days mon,tue,thu                         # custom work days
py keep_awake.py --us-holidays --holiday 2026-12-24         # days off
py keep_awake.py --input none                               # keep awake only; Teams may go Away
py keep_awake.py --input mouse                              # use the 1-px mouse nudge only
py keep_awake.py --always-awake                             # never sleep, even off-hours (no input)
py keep_awake.py --sleep-on-breaks                          # allow sleep during breaks too
py keep_awake.py --log-file "%LOCALAPPDATA%\keep_awake.log"
py keep_awake.py --dry-run                                  # simulate on any OS
```

To make your options permanent, edit the `set "ARGS=..."` line in `KeepAwake.cmd`. Times are the PC's local time.

## How it works, and why "awake" and "Teams green" are different

Windows has two separate timers, so the script uses two mechanisms:

1. **Machine awake (sleep and display timeout).** This uses [`SetThreadExecutionState`](https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate)`(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)`. It's the documented API that video players, presentation tools and PowerToys Awake use. It sends no input and changes no settings, and Windows drops it automatically when the process exits.

2. **Teams presence and screen lock.** Teams sets you to [Away after about 5 minutes without keyboard or mouse input](https://learn.microsoft.com/microsoftteams/presence-admins), or as soon as the PC locks. It reads the same *last input* idle timer that drives the screen saver and the inactivity lock. **A power request does not reset that timer**, so mechanism 1 alone leaves you Away. During work hours, once you've been idle for 2 minutes, the script sends a single **F24** key tap. F24 isn't on any physical keyboard, so it types nothing, moves nothing and doesn't steal focus. The script then checks with `GetLastInputInfo` that Windows registered the tap. If it didn't (some VMs or remote consoles), it switches to a 1-pixel mouse move that returns to the same spot. If that doesn't register either, it backs off for 5 minutes instead of looping.

During a **break** no input is sent, so Teams goes Away after its normal timeout while the PC itself stays awake. When you come back and touch the keyboard, Teams turns green again right away.

The 2-minute threshold is lowered automatically when a shorter lock timeout is found (to half of it, but never below 10 s). It checks the screen saver, the `InactivityTimeoutSecs` Group Policy, the Group Policy screen-saver timeout and the Intune/MDM `MaxInactivityTimeDeviceLock` setting.

## Designed to be quiet with security tools

Endpoint protection products flag malware-style behaviour. This script doesn't do any of that:

- **No** keyboard or mouse hooks, keylogging, injection into other processes, admin rights, or UAC prompts.
- **No** registry, policy or power-plan writes. It only *reads* the screen-saver and lock-timeout settings.
- **No** network access, downloads, services, scheduled tasks, or hidden persistence. Autostart is a shortcut you add yourself.
- **No** obfuscation or packed executables. It's plain, readable Python run by the standard `python.exe`, and it shows a visible console window.
- Input is sent only while you're idle, only on the normal unlocked desktop, and never while a key or mouse button is held. It never acts on the lock screen or a UAC prompt.

It doesn't try to hide from or get around security software. If your organisation blocks Python or simulated input by policy, respect that. Use this only on devices and in ways your employer allows.

## Limits

- It can't override a manually set Teams status, a meeting or call status, calendar-based status, or org presence policies.
- An enforced policy can still lock the screen. Closing the lid, critical battery, Dynamic Lock and a manual `Win+L` still work as usual.
- Run it in the **same Windows session as Teams**. If Teams runs inside a VM or remote desktop, run the script there, not on the host.
- If an *elevated* (admin) window is in front, Windows blocks simulated input. The script retries instead of spinning.
- **On VMs, RDP and Citrix:** a *minimised* remote window has no interactive desktop, so the script pauses input until the window is restored. Keep the session window open, even behind other windows.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests fake every Windows call, so they run on any OS. CI runs them on Windows and Ubuntu, and on Windows it also makes a real power request.

## License

MIT. See [LICENSE](LICENSE).
