@echo off
rem on-stop.cmd - Stop hook entry: run record_usage.py to append per-session stats.
rem record_usage.py is fault-tolerant: exceptions are swallowed to stats.log and
rem stdout is always valid empty JSON. The `>> cmd-stderr.log` redirect captures
rem any stray output; `echo {}` guarantees the hook stdout is always valid JSON
rem even if the script did not run. exit /b 0 keeps Stop from ever failing.
rem
rem Path resolution (portable, no hardcoded machine paths):
rem   - ZCODE_PLUGIN_ROOT: injected by ZCode for plugin hooks; when not present
rem     (manual run) it is derived from this script location (hooks\.. = plugin
rem     root).
rem   - Data dir: %USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats
rem   - Python: PYTHON_BIN env var > python.exe on PATH > py -3 launcher.
rem     When nothing is found, a hint goes to stderr and the hook still exits 0.

setlocal EnableExtensions

if not defined ZCODE_PLUGIN_ROOT (
    for %%I in ("%~dp0..") do set "ZCODE_PLUGIN_ROOT=%%~fI"
)

set "DATA=%USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats"

if not exist "%DATA%" mkdir "%DATA%"

rem ---- Resolve Python interpreter (three-tier fallback) ----
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

rem Use a separate cmd-stderr.log for the redirect target. stats.log is written
rem by record_usage.py itself. On Windows, keeping them separate avoids a
rem file-sharing conflict: cmd's 2>> handle holds the file with exclusive write
rem access, which makes the script's own append open() fail silently.

%PY_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\record_usage.py" "%~1" >nul 2>> "%DATA%\cmd-stderr.log"

echo {}
exit /b 0
