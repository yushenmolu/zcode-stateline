@echo off
rem uninstall.cmd - Remove zcode-token-stats from the local ZCode plugin cache.
rem
rem What it does:
rem   1. Removes the install record from <ZCODE_HOME>\cli\plugins\installed_plugins.json
rem      (backs up the original file first as .bak-<timestamp>, then validates JSON)
rem   2. Removes the enable entry from <ZCODE_HOME>\cli\config.json
rem      (plugins.enabledPlugins, same backup + validate treatment)
rem   3. Deletes the plugin cache directory
rem      <ZCODE_HOME>\cli\plugins\cache\local\zcode-token-stats (all versions)
rem   4. Reminds you to restart ZCode
rem
rem NOT touched by design: the plugin data directory
rem   <ZCODE_HOME>\cli\plugins\data\local\zcode-token-stats
rem   (long-term stats history token-stats.jsonl etc.). Delete it manually
rem   if you really want a full wipe.
rem
rem Usage:
rem   uninstall.cmd        real uninstall
rem   uninstall.cmd /dry   preview only: print every planned action, change nothing
rem
rem Environment overrides:
rem   ZCODE_HOME   ZCode data root. Default: %USERPROFILE%\.zcode
rem   PYTHON_BIN   Python interpreter. Default: python on PATH, then py -3

setlocal EnableExtensions

set "DRY=0"
if /i "%~1"=="/dry" set "DRY=1"

if not defined ZCODE_HOME set "ZCODE_HOME=%USERPROFILE%\.zcode"
set "CACHE=%ZCODE_HOME%\cli\plugins\cache\local\zcode-token-stats"
set "IP_JSON=%ZCODE_HOME%\cli\plugins\installed_plugins.json"
set "CFG_JSON=%ZCODE_HOME%\cli\config.json"

echo ============================================================
echo  zcode-token-stats uninstaller
if "%DRY%"=="1" (echo  mode       : DRY RUN - preview only, nothing is changed) else (echo  mode       : REAL UNINSTALL)
echo  zcode home : %ZCODE_HOME%
echo  plugin dir : %CACHE%
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
    echo [uninstall] ERROR: Python interpreter not found. 1>&2
    echo [uninstall]        Install Python 3.10+ from python.org, or set PYTHON_BIN to python.exe. 1>&2
    exit /b 1
)
echo [uninstall] Python resolved: %PY_CMD%
echo.

rem ============================================================
rem Step 1/3: remove the install record from installed_plugins.json
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 1/3: would update %IP_JSON%
    echo [DRY]   - back it up to installed_plugins.json.bak-^<timestamp^>
    echo [DRY]   - remove the record with id=zcode-token-stats@local if present
) else (
    echo [uninstall] Step 1/3: updating installed_plugins.json ...
    %PY_CMD% -c "import json,sys,time,shutil,os; p=sys.argv[1]; d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {}; a=d.setdefault('plugins',[]); a[:] =[x for x in a if x.get('id')!='zcode-token-stats@local']; os.path.exists(p) and shutil.copy2(p,p+'.bak-'+time.strftime('%%Y%%m%%d-%%H%%M%%S')); t=p+'.tmp'; f=open(t,'w',encoding='utf-8'); json.dump(d,f,ensure_ascii=False,indent=2); f.close(); g=open(t,encoding='utf-8'); json.load(g); g.close(); os.replace(t,p); print('[uninstall] installed_plugins.json updated, JSON valid')" "%IP_JSON%"
    if errorlevel 1 (
        echo [uninstall] ERROR: failed to update installed_plugins.json. 1>&2
        exit /b 1
    )
)
echo.

rem ============================================================
rem Step 2/3: remove the enable entry from config.json
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 2/3: would update %CFG_JSON%
    echo [DRY]   - back it up to config.json.bak-^<timestamp^>
    echo [DRY]   - remove plugins.enabledPlugins["zcode-token-stats@local"]
) else (
    echo [uninstall] Step 2/3: updating config.json ...
    %PY_CMD% -c "import json,sys,time,shutil,os; p=sys.argv[1]; d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {}; d.get('plugins',{}).get('enabledPlugins',{}).pop('zcode-token-stats@local',None); os.path.exists(p) and shutil.copy2(p,p+'.bak-'+time.strftime('%%Y%%m%%d-%%H%%M%%S')); t=p+'.tmp'; f=open(t,'w',encoding='utf-8'); json.dump(d,f,ensure_ascii=False,indent=2); f.close(); g=open(t,encoding='utf-8'); json.load(g); g.close(); os.replace(t,p); print('[uninstall] config.json updated, JSON valid')" "%CFG_JSON%"
    if errorlevel 1 (
        echo [uninstall] ERROR: failed to update config.json. 1>&2
        exit /b 1
    )
)
echo.

rem ============================================================
rem Step 3/3: delete the plugin cache directory
rem ============================================================
if "%DRY%"=="1" (
    echo [DRY] Step 3/3: would delete directory:
    echo [DRY]   %CACHE%
    if exist "%CACHE%" (
        echo [DRY]   it currently contains these version dirs:
        for /d %%D in ("%CACHE%\*") do echo [DRY]     %%~nxD
    ) else (
        echo [DRY]   directory does not exist, nothing to delete
    )
) else (
    if exist "%CACHE%" (
        echo [uninstall] Step 3/3: deleting plugin cache directory ...
        rmdir /s /q "%CACHE%"
        if exist "%CACHE%" (
            echo [uninstall] ERROR: could not fully delete %CACHE% . 1>&2
            exit /b 1
        )
        echo [uninstall] Step 3/3: plugin cache directory deleted.
    ) else (
        echo [uninstall] Step 3/3: plugin cache directory not found, skip.
    )
)
echo.

echo ============================================================
if "%DRY%"=="1" (
    echo [DRY] Preview finished. Nothing was changed.
    echo [DRY] Run without /dry to perform the real uninstall.
) else (
    echo [uninstall] Done. Please fully quit and restart ZCode.
    echo [uninstall] Kept by design: stats data dir
    echo [uninstall]   %ZCODE_HOME%\cli\plugins\data\local\zcode-token-stats
    echo [uninstall] Delete it manually if you want a full wipe.
)
echo ============================================================
endlocal
exit /b 0
