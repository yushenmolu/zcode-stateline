@echo off
rem status-event.cmd - status timing hook entry: record the current session
rem activity phase (generating / tool / idle) into status-state.json via
rem scripts\status_event.py, so the docked status bar can infer the status
rem badge without the live-stream proxy.
rem
rem Design notes:
rem  - Mirrors mark-session.cmd conventions: setlocal, ZCODE_PLUGIN_ROOT
rem    fallback, PYTHON_BIN default, exit /b 0 so the hook can never block.
rem  - %~1 = event name (generating / tool / idle), %~2 = session id from
rem    ${ZCODE_SESSION_ID} (fallback only); stdin carries the hook input JSON
rem    (read first by status_event.py).
rem  - status_event.py always prints exactly one valid JSON object "{}" and
rem    exits 0; `echo {}` covers the case where Python cannot start at all.
rem  - stderr is appended to the data dir so stray output never pollutes stdout.

setlocal

rem ZCODE_PLUGIN_ROOT is injected by ZCode as an environment variable for plugin
rem hooks; fall back to the absolute path so this also works when run by hand
rem outside of ZCode (e.g. manual verification).
if not defined ZCODE_PLUGIN_ROOT set "ZCODE_PLUGIN_ROOT=C:\Users\yushe\.zcode\cli\plugins\cache\local\zcode-token-stats\0.1.0"

rem PYTHON_BIN can be overridden in the environment; default to this machine's
rem Python 3 interpreter at D:\Program Files\python312\python.exe.
if not defined PYTHON_BIN set "PYTHON_BIN=D:\Program Files\python312\python.exe"

set "DATA=C:\Users\yushe\.zcode\cli\plugins\data\local\zcode-token-stats"

if not exist "%DATA%" mkdir "%DATA%"

"%PYTHON_BIN%" "%ZCODE_PLUGIN_ROOT%\scripts\status_event.py" "%~1" "%~2" 2>> "%DATA%\status-event-stderr.log" || echo {}

exit /b 0
