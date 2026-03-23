@echo off
REM mkimage.bat — Launcher for mkimage
REM
REM Tries Python (mkimage.pyz) first for the modern GUI.
REM Falls back to PowerShell (mkimage.ps1) if Python not available.
REM
REM Usage:
REM     mkimage.bat              Launch GUI
REM     mkimage.bat --help       Show CLI help
REM     mkimage.bat [args...]    Pass arguments to mkimage

setlocal

REM Unblock scripts if downloaded from the internet
powershell -NoProfile -Command "Get-Item '%~dp0mkimage.pyz','%~dp0mkimage.ps1','%~dp0mkimage.py' -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

REM Try Python with mkimage.pyz first
where python >nul 2>&1
if %errorlevel% equ 0 (
    if exist "%~dp0mkimage.pyz" (
        python "%~dp0mkimage.pyz" %*
        exit /b %errorlevel%
    )
)

REM Fall back to PowerShell GUI
if exist "%~dp0mkimage.ps1" (
    powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0mkimage.ps1" %*
    if %errorlevel% neq 0 (
        echo.
        echo PowerShell exited with error code %errorlevel%
        echo If you see "cannot be loaded because running scripts is disabled":
        echo   Run: Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
        echo.
        pause
    )
    exit /b %errorlevel%
)

echo Error: Neither Python nor mkimage.ps1 found.
echo Install Python from https://python.org or ensure mkimage.ps1 is present.
pause
exit /b 1
