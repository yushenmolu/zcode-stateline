@echo off
rem inject-context.cmd - SessionStart / UserPromptSubmit hook entry: run
rem inject_context.py and pass its stdout straight to the hook runner.
rem
rem Design notes:
rem  - inject_context.py always prints exactly one strict JSON object
rem    ({"additionalContext": ...}) and always exits 0, so hooking it directly
rem    is the most reliable way to put that JSON on the hook's stdout. An
rem    intermediate file + `type` is NOT used: cmd's stdout redirect creates
rem    the target file before the script starts, so if the script cannot run
rem    at all (missing interpreter/path) the file exists but is EMPTY and
rem    `type` would print an empty line instead of valid JSON.
rem  - `|| echo {}` covers the "script never started" case: the error message
rem    goes to stderr (isolated via 2>>), nothing invalid lands on stdout, and
rem    `echo {}` guarantees the hook always sees valid JSON.
rem  - stderr is appended to the data dir so stray output never pollutes
rem    stdout and the hook never fails.
rem  - exit /b 0 keeps SessionStart / UserPromptSubmit from ever blocking.
rem  - Paths are derived at runtime (no hardcoded machine paths): plugin root
rem    from ZCODE_PLUGIN_ROOT or this script location, data dir from
rem    %USERPROFILE%, Python via PYTHON_BIN > where python > py -3.

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

rem %1 = event kind: session-start | user-prompt-submit. The hooks.json wiring
rem always passes one of these; guard an empty argument with the default.
set "EVENT=%~1"
if "%EVENT%"=="" set "EVENT=session-start"

%PY_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\inject_context.py" --event %EVENT% 2>> "%DATA%\inject-stderr.log" || echo {}

exit /b 0
