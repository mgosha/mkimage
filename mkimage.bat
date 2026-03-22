@echo off
REM mkimage.bat — Launcher for mkimage PowerShell GUI
REM
REM No external dependencies — uses native Windows APIs.
REM
REM Usage:
REM     mkimage.bat              Launch GUI
REM     mkimage.bat --gui        Launch GUI (explicit)

setlocal

REM Unblock scripts if they were downloaded from the internet
powershell -NoProfile -Command "Get-Item '%~dp0mkimage.ps1','%~dp0mkimage.py' -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

REM Launch the PowerShell GUI
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0mkimage.ps1"
if %errorlevel% neq 0 (
    echo.
    echo PowerShell exited with error code %errorlevel%
    echo If you see "cannot be loaded because running scripts is disabled":
    echo   Run: Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
    echo.
    pause
)
exit /b %errorlevel%
