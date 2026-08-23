@echo off
rem ensure-docked-statusbar.cmd - SessionStart hook entry: make sure the docked
rem token status bar (scripts/docked_statusbar.py) is running.
rem
rem Design notes:
rem  - Uses pythonw.exe (no console window) and `start` (fire-and-forget), so the
rem    status bar runs detached from the hook / ZCode lifetime. If it was already
rem    started (a live statusbar.pid exists), this is a no-op to avoid duplicate
rem    bars.
rem  - stdout is stdlib "{}" always, so the hook always sees valid JSON; the
rem    status bar's own show/hide logic handles the GUI. exit /b 0 keeps
rem    SessionStart from ever blocking.
rem  - Side effect: the *.cmd always prints {} and exits 0, so it stays safe even
rem    when run by hand (manual fallback start).
rem  - Paths are derived at runtime (no hardcoded machine paths): plugin root
rem    from ZCODE_PLUGIN_ROOT or this script location, data dir from
rem    %USERPROFILE%, Python via PYTHONW_BIN/PYTHON_BIN > where pythonw/python
rem    > py -3 (pythonw.exe lives in the same directory as python.exe).

setlocal EnableExtensions

rem ZCODE_PLUGIN_ROOT is injected by ZCode as an environment variable for
rem plugin hooks; when absent (manual run) derive it from this script location.
if not defined ZCODE_PLUGIN_ROOT (
    for %%I in ("%~dp0..") do set "ZCODE_PLUGIN_ROOT=%%~fI"
)

set "DATA=%USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats"

if not exist "%DATA%" mkdir "%DATA%"

rem ---- Resolve console Python (three-tier fallback) ----
rem 1) PYTHON_BIN env var   2) python.exe on PATH   3) py -3 launcher
set "PY_CMD="
if defined PYTHON_BIN set "PY_CMD="%PYTHON_BIN%""
if not defined PY_CMD (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY_CMD set "PY_CMD="%%P""
    )
)
if not defined PY_CMD (
    where py >nul 2>nul && set "PY_CMD=py -3"
)
if not defined PY_CMD (
    echo [zcode-token-stats] Python interpreter not found. 1>&2
    echo [zcode-token-stats] Please install Python 3.10+ from python.org or set the PYTHON_BIN environment variable to python.exe. 1>&2
    echo {}
    exit /b 0
)

rem ---- Resolve windowed Python (pythonw.exe, no console window) ----
rem 1) PYTHONW_BIN env var   2) pythonw.exe on PATH
rem 3) pythonw.exe next to the resolved python.exe (same install directory)
rem 4) fall back to PY_CMD (the bar still works, just with a console window)
set "PYW_CMD="
if defined PYTHONW_BIN set "PYW_CMD="%PYTHONW_BIN%""
if not defined PYW_CMD (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do (
        if not defined PYW_CMD set "PYW_CMD="%%P""
    )
)
if not defined PYW_CMD (
    echo %PY_CMD% | findstr /i /c:"python.exe" >nul && set "PYW_CMD=%PY_CMD:python.exe=pythonw.exe%"
)
if not defined PYW_CMD set "PYW_CMD=%PY_CMD%"

rem Launch only when no live instance. statusbar_alive.py reads statusbar.pid:
rem exit 0 = the recorded pid is alive (skip, avoid duplicate bars); otherwise
rem it deletes any stale pid file and exits 1 -> `||` starts a fresh bar.
%PY_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\statusbar_alive.py" || start "" %PYW_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\docked_statusbar.py"

:done
rem Always emit valid JSON to the hook runner and never block.
echo {}
exit /b 0
