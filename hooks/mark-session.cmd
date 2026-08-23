@echo off
rem mark-session.cmd - SessionStart / UserPromptSubmit hook entry: persist the
rem current real ZCode session id (injected by ZCode as ${CLAUDE_SESSION_ID})
rem into the data dir (current-session.json) so the docked status bar knows
rem which conversation to show stats for.
rem
rem Design notes:
rem  - Mirrors inject-context.cmd's conventions: setlocal, ZCODE_PLUGIN_ROOT
rem    derived at runtime, three-tier Python resolution, `|| echo {}` for the
rem    "script never started" case, `exit /b 0` so the hook can never block.
rem  - mark_session.py always prints exactly one valid JSON object "{}" and
rem    exits 0; `echo {}` covers the case where Python cannot start at all.
rem  - stderr is appended to the data dir so stray output never pollutes stdout.
rem  - %~1 is the session id passed by the hook; extra arguments are ignored.
rem    mark_session.py reads the hook input JSON from stdin FIRST (official
rem    hook input mechanism, common field session_id) and falls back to %~1
rem    only when stdin yields nothing, so the stdin pipe must stay untouched
rem    here (no <nul, no redirect of stdin).

setlocal EnableExtensions

rem ZCODE_PLUGIN_ROOT is injected by ZCode as an environment variable for
rem plugin hooks; when absent (manual run) derive it from this script location.
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

rem %1 = session id from the hook; stdin carries the hook input JSON (read
rem first by mark_session.py); %~1 empty is handled there.
%PY_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\mark_session.py" "%~1" 2>> "%DATA%\mark-session-stderr.log" || echo {}

exit /b 0
