@echo off
rem ensure-proxy.cmd - SessionStart hook entry: make sure the local SSE reverse
rem proxy (scripts/proxy_server.py, 127.0.0.1:18080 -> 127.0.0.1:8080) is
rem listening, so the docked status bar can show live "generating" token
rem counts via live_stream.json.
rem
rem Design notes:
rem  - Idempotent: probes the listen port first; if something already listens
rem    on 18080 this is a no-op (no duplicate proxy processes).
rem  - Uses pythonw.exe (no console window) and `start` (fire-and-forget), so
rem    the proxy runs detached from the hook / ZCode lifetime.
rem  - stdout is stdlib "{}" always, so the hook always sees valid JSON; exit
rem    /b 0 keeps SessionStart from ever blocking.
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
rem 4) fall back to PY_CMD (the proxy still works, just with a console window)
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

rem ---- Idempotent launch: probe 127.0.0.1:18080, start proxy only when free ----
%PY_CMD% -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1',18080))==0 else 1)" >nul 2>&1
if errorlevel 1 (
    start "" %PYW_CMD% "%ZCODE_PLUGIN_ROOT%\scripts\proxy_server.py"
)

:done
rem Always emit valid JSON to the hook runner and never block.
echo {}
exit /b 0
