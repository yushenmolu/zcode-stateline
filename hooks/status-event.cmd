@echo off
rem status-event.cmd - status timing hook entry: record the current session
rem activity phase (generating / tool / idle) into status-state.json via
rem scripts\status_event.py, so the docked status bar can infer the status
rem badge without the live-stream proxy.
rem
rem Design notes:
rem  - Mirrors mark-session.cmd / ensure-docked-statusbar.cmd conventions:
rem    setlocal EnableExtensions, ZCODE_PLUGIN_ROOT derived at runtime when the
rem    env var is absent, Python resolved via PYTHON_BIN > where python > py -3,
rem    exit /b 0 so the hook can never block.
rem  - %~1 = event name (generating / tool / idle), %~2 = session id from
rem    ${CLAUDE_SESSION_ID} (fallback only), %~3 = platform hook name (also
rem    fallback: status_event.py prefers stdin's hook_event_name). The hook name
rem    lets the writer mark turn start (UserPromptSubmit) and shard per session.
rem  - status_event.py always prints exactly one valid JSON object "{}" and
rem    exits 0; `echo {}` covers the case where Python cannot start at all.
rem  - stderr is appended to the data dir so stray output never pollutes stdout.

setlocal EnableExtensions

rem ZCODE_PLUGIN_ROOT is injected by ZCode as an environment variable for plugin
rem hooks; when absent (manual run) derive it from this script location.
if not defined ZCODE_PLUGIN_ROOT (
    for %%I in ("%~dp0..") do set "ZCODE_PLUGIN_ROOT=%%~fI"
)

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

rem ---- Data dir (derived at runtime, no machine-specific path) ----
set "DATA=%USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats"

if not exist "%DATA%" mkdir "%DATA%"

%PY_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\status_event.py" "%~1" "%~2" "%~3" 2>> "%DATA%\status-event-stderr.log" || echo {}

exit /b 0
