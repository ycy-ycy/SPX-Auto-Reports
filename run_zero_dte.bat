@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Project Python is missing. Follow the Windows setup in README.md first.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -B "%~dp0tools\run_report.py" zero-dte
set "REPORT_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %REPORT_EXIT%
