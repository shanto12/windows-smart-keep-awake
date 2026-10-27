@echo off
rem Windows Smart Keep Awake launcher. Double-click to start; close the window to stop.
rem Edit ARGS to change the schedule, e.g. set ARGS=--hours 07:30-17:00 --break 12:30-13:30
set "ARGS="
title Windows Smart Keep Awake
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 keep_awake.py %ARGS% %*
    goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python keep_awake.py %ARGS% %*
    goto :done
)
echo Python 3.9 or newer is required: https://www.python.org/downloads/windows/
echo (or: winget install Python.Python.3.12)
:done
if not "%errorlevel%"=="0" pause
