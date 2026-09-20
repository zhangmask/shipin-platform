@echo off
rem =====================================================================
rem  Shipin Platform - desktop entry (Windows)
rem  Double-click to open the platform as a native desktop window.
rem  Falls back to system browser if WebView2/pywebview unavailable.
rem =====================================================================
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found on PATH. Install Python 3.11+ first.
    pause
    exit /b 1
)
python tools\desktop_app.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] desktop app exited with an error - see data\desktop_server.log
    pause
)