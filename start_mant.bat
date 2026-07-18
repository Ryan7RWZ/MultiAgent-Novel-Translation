@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Always run from the repository root, including when launched by double-click.
cd /d "%~dp0"

set "MANT_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "MANT_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
set "MANT_CONFIG=config\settings.yaml"
set "MANT_MONITOR_URL=http://127.0.0.1:8765"

if /i "%~1"=="--check" goto :check

if not exist "%MANT_CONFIG%" (
    echo [ERROR] Missing %MANT_CONFIG%.
    echo Copy config\settings.example.yaml to config\settings.yaml first.
    pause
    exit /b 1
)

"%MANT_PYTHON%" -c "import mant" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python cannot import mant.
    echo Install dependencies with: python -m pip install -e ".[dev]"
    pause
    exit /b 1
)

rem Refresh the persistent DeepSeek user variable when this process predates setx/UI changes.
if not defined DEEPSEEK_API_KEY (
    for /f "delims=" %%K in ('powershell -NoProfile -Command "$v=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User'); if ($v) { $v }"') do set "DEEPSEEK_API_KEY=%%K"
)
if defined DEEPSEEK_API_KEY (
    echo [MANT] DeepSeek API key detected in the process environment.
) else (
    echo [WARN] DEEPSEEK_API_KEY is not available; translation will use DRAFT fallback.
)

if not "%~1"=="" (
    if not exist "%~f1" (
        echo [ERROR] Chapter file does not exist: %~f1
        pause
        exit /b 1
    )
)

echo [MANT] Starting Agent monitor...
start "MANT Agent Monitor" /min "%ComSpec%" /k ""%MANT_PYTHON%" -m mant.cli monitor --config "%MANT_CONFIG%""
powershell -NoProfile -Command "$limit=(Get-Date).AddSeconds(10); do { try { $r=Invoke-WebRequest -UseBasicParsing -Uri '%MANT_MONITOR_URL%/api/health' -TimeoutSec 1; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 400 } while ((Get-Date) -lt $limit); exit 1"
if errorlevel 1 echo [WARN] Monitor health check timed out; inspect the minimized monitor window.
start "" "%MANT_MONITOR_URL%"

rem No chapter argument: dashboard-only one-click mode.
if "%~1"=="" (
    echo [MANT] Dashboard opened: %MANT_MONITOR_URL%
    echo [MANT] Drag a chapter file onto start_mant.bat to monitor a translation automatically.
    exit /b 0
)

set "MANT_INPUT=%~f1"
set "MANT_WORK_ID=demo_work"
if not "%~2"=="" set "MANT_WORK_ID=%~2"

for %%I in ("%MANT_INPUT%") do set "MANT_CHAPTER_ID=%%~nI"
if not "%~3"=="" set "MANT_CHAPTER_ID=%~3"

echo [MANT] Work: %MANT_WORK_ID%
echo [MANT] Chapter: %MANT_CHAPTER_ID%
echo [MANT] Input: %MANT_INPUT%
echo.

"%MANT_PYTHON%" -m mant.cli translate-chapter ^
    --config "%MANT_CONFIG%" ^
    --work-id "%MANT_WORK_ID%" ^
    --chapter-id "%MANT_CHAPTER_ID%" ^
    --input "%MANT_INPUT%" ^
    --stream --verbose --trace

set "MANT_EXIT_CODE=%ERRORLEVEL%"
echo.
if "%MANT_EXIT_CODE%"=="0" (
    echo [MANT] Translation completed. Keep the monitor window open to inspect the run.
) else (
    echo [ERROR] Translation failed with exit code %MANT_EXIT_CODE%.
)
pause
exit /b %MANT_EXIT_CODE%

:check
if not exist "%MANT_CONFIG%" (
    echo CHECK_CONFIG=missing
    exit /b 1
)
"%MANT_PYTHON%" -c "import mant, mant.cli, mant.observability.dashboard"
if errorlevel 1 (
    echo CHECK_IMPORT=failed
    exit /b 1
)
echo CHECK_CONFIG=ok
echo CHECK_IMPORT=ok
echo CHECK_PYTHON=%MANT_PYTHON%
exit /b 0
