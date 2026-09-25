@echo off
rem install.cmd - Install zcode-token-stats into the local ZCode plugin cache.
rem
rem What it does:
rem   1. Copies this project to <ZCODE_HOME>\cli\plugins\cache\local\zcode-token-stats\<VERSION>
rem      (skips .git / __pycache__ / *.pyc)
rem   2. Adds the install record to <ZCODE_HOME>\cli\plugins\installed_plugins.json
rem      - backs up the original file first (.bak-<timestamp>)
rem      - idempotent: any existing zcode-token-stats record is removed first,
rem        then the new one is appended
rem      - the file is re-parsed after writing to guarantee valid JSON
rem   3. Enables the plugin in <ZCODE_HOME>\cli\config.json (plugins.enabledPlugins),
rem      with the same backup + validate treatment
rem   4. Registers scheduled task "ZcodeTokenStatsAlive" (every 5 minutes) pointing at
rem      the deployed watchdog scripts\statusbar_alive.py, so the docked status bar
rem      gets a mutex-guarded heartbeat even when no new ZCode session starts
rem   5. Reminds you to restart ZCode
rem
rem Usage:
rem   install.cmd        real install
rem   install.cmd /dry   preview only: print every planned action, change nothing
rem
rem Environment overrides:
rem   ZCODE_HOME   ZCode data root. Default: %USERPROFILE%\.zcode
rem   PYTHON_BIN   Python interpreter. Default: python on PATH, then py -3

setlocal EnableExtensions

rem Keep in sync with .zcode-plugin\plugin.json
set "VERSION=0.12.5"

set "DRY=0"
if /i "%~1"=="/dry" set "DRY=1"

rem Source = the directory containing this script (project root)
set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

if not exist "%SRC%\.zcode-plugin\plugin.json" (
    echo [install] ERROR: .zcode-plugin\plugin.json not found next to this script. 1>&2
    exit /b 1
)

if not defined ZCODE_HOME set "ZCODE_HOME=%USERPROFILE%\.zcode"
set "CACHE=%ZCODE_HOME%\cli\plugins\cache\local\zcode-token-stats"
set "DST=%CACHE%\%VERSION%"
set "IP_JSON=%ZCODE_HOME%\cli\plugins\installed_plugins.json"
set "CFG_JSON=%ZCODE_HOME%\cli\config.json"

echo ============================================================
echo  zcode-token-stats v%VERSION% installer
if "%DRY%"=="1" (echo  mode       : DRY RUN - preview only, nothing is changed) else (echo  mode       : REAL INSTALL)
echo  source     : %SRC%
echo  zcode home : %ZCODE_HOME%
echo  target     : %DST%
echo ============================================================

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
    echo [install] ERROR: Python interpreter not found. 1>&2
    echo [install]        Install Python 3.10+ from python.org, or set PYTHON_BIN to python.exe. 1>&2
    exit /b 1
)
echo [install] Python resolved: %PY_CMD%

rem ---- Resolve windowed Python (pythonw.exe) for the watchdog scheduled task ----
rem 1) PYTHONW_BIN env var   2) pythonw.exe on PATH   3) pythonw.exe next to the
rem resolved python.exe (same install directory)   4) bare pythonw.exe (PATH)
set "PYW_EXE="
if defined PYTHONW_BIN set "PYW_EXE=%PYTHONW_BIN%"
if not defined PYW_EXE (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do (
        if not defined PYW_EXE set "PYW_EXE=%%P"
    )
)
if not defined PYW_EXE (
    echo %PY_CMD% | findstr /i /c:"python.exe" >nul && set "PYW_EXE=%PY_CMD:python.exe=pythonw.exe%"
)
if not defined PYW_EXE set "PYW_EXE=pythonw.exe"
set "PYW_EXE=%PYW_EXE:"=%"
echo [install] Pythonw resolved: %PYW_EXE%
echo.

rem ============================================================
rem Step 1/4: copy project files into the plugin cache
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 1/4: would copy project files:
    robocopy "%SRC%" "%DST%" /E /L /NP /XD .git __pycache__ /XF *.pyc *.bak-*
    echo [DRY] robocopy exit code above is from list-only mode ^(no files copied^).
) else (
    echo [install] Step 1/4: copying project files to plugin cache ...
    robocopy "%SRC%" "%DST%" /E /NP /NFL /NDL /NJH /NJS /XD .git __pycache__ /XF *.pyc *.bak-* >nul
    if errorlevel 8 (
        echo [install] ERROR: robocopy failed with code %ERRORLEVEL%. 1>&2
        exit /b 1
    )
    echo [install] Step 1/4: copy done.
)
echo.

rem ============================================================
rem Step 2/4: register the plugin in installed_plugins.json
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 2/4: would update %IP_JSON%
    echo [DRY]   - back it up to installed_plugins.json.bak-^<timestamp^>
    echo [DRY]   - remove any existing zcode-token-stats@local record ^(idempotent^)
    echo [DRY]   - append record: id=zcode-token-stats@local version=%VERSION% installPath=%DST%
) else (
    echo [install] Step 2/4: updating installed_plugins.json ...
    %PY_CMD% -c "import json,sys,time,shutil,os; p,ip,ver=sys.argv[1],sys.argv[2],sys.argv[3]; d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {}; a=d.setdefault('plugins',[]); a[:] =[x for x in a if x.get('id')!='zcode-token-stats@local']; now=time.strftime('%%Y-%%m-%%dT%%H:%%M:%%S.000Z',time.gmtime()); a.append({'id':'zcode-token-stats@local','name':'zcode-token-stats','marketplace':'local','version':ver,'installPath':ip,'installedAt':now,'updatedAt':now,'scope':'user','source':'./plugins/zcode-token-stats'}); os.path.isdir(os.path.dirname(p)) or os.makedirs(os.path.dirname(p),exist_ok=True); os.path.exists(p) and shutil.copy2(p,p+'.bak-'+time.strftime('%%Y%%m%%d-%%H%%M%%S')); t=p+'.tmp'; f=open(t,'w',encoding='utf-8'); json.dump(d,f,ensure_ascii=False,indent=2); f.close(); g=open(t,encoding='utf-8'); json.load(g); g.close(); os.replace(t,p); print('[install] installed_plugins.json updated, JSON valid')" "%IP_JSON%" "%DST%" "%VERSION%"
    if errorlevel 1 (
        echo [install] ERROR: failed to update installed_plugins.json. 1>&2
        echo [install]        A backup with suffix .bak-* may exist next to it; restore if needed. 1>&2
        exit /b 1
    )
)
echo.

rem ============================================================
rem Step 3/4: enable the plugin in config.json
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 3/4: would update %CFG_JSON%
    echo [DRY]   - back it up to config.json.bak-^<timestamp^>
    echo [DRY]   - set plugins.enabledPlugins["zcode-token-stats@local"] = true
) else (
    echo [install] Step 3/4: enabling plugin in config.json ...
    %PY_CMD% -c "import json,sys,time,shutil,os; p=sys.argv[1]; d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {}; d.setdefault('plugins',{}).setdefault('enabledPlugins',{})['zcode-token-stats@local']=True; os.path.isdir(os.path.dirname(p)) or os.makedirs(os.path.dirname(p),exist_ok=True); os.path.exists(p) and shutil.copy2(p,p+'.bak-'+time.strftime('%%Y%%m%%d-%%H%%M%%S')); t=p+'.tmp'; f=open(t,'w',encoding='utf-8'); json.dump(d,f,ensure_ascii=False,indent=2); f.close(); g=open(t,encoding='utf-8'); json.load(g); g.close(); os.replace(t,p); print('[install] config.json updated, JSON valid')" "%CFG_JSON%"
    if errorlevel 1 (
        echo [install] ERROR: failed to update config.json. 1>&2
        echo [install]        A backup with suffix .bak-* may exist next to it; restore if needed. 1>&2
        exit /b 1
    )
)
echo.

rem ============================================================
rem Step 4/4: register the watchdog scheduled task (5-minute heartbeat)
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 4/4: would register scheduled task "ZcodeTokenStatsAlive":
    echo [DRY]   schtasks /create /sc minute /mo 5 /f
    echo [DRY]   /tr "%PYW_EXE%" "%DST%\scripts\statusbar_alive.py"
) else (
    echo [install] Step 4/4: registering watchdog scheduled task ...
    schtasks /create /tn "ZcodeTokenStatsAlive" /sc minute /mo 5 /tr "\"%PYW_EXE%\" \"%DST%\scripts\statusbar_alive.py\"" /f
    if errorlevel 1 (
        echo [install] WARNING: schtasks failed, watchdog heartbeat not registered. 1>&2
        echo [install]          The status bar still starts via the SessionStart hook. 1>&2
    ) else (
        echo [install] Step 4/4: scheduled task registered ^(runs every 5 minutes^).
    )
)
echo.

echo ============================================================
if "%DRY%"=="1" (
    echo [DRY] Preview finished. Nothing was changed.
    echo [DRY] Run without /dry to perform the real install.
) else (
    echo [install] Done. Please fully quit and restart ZCode so the
    echo [install] hooks, /stats command and status bar take effect.
)
echo ============================================================
endlocal
exit /b 0
