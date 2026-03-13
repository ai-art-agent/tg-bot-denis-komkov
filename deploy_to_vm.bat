@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: Window 1: tests (stays open)
start "Tests bot" cmd /k "cd /d ""%~dp0"" && python tests_bot.py"

:: Window 2: PowerShell 7 (local commit/push), then VM window
start "Deploy local" "C:\Program Files\PowerShell\7-preview\pwsh.exe" -NoExit -ExecutionPolicy Bypass -File "%~dp0deploy_local.ps1"

echo.
echo Test and deploy windows opened. You can close this window.
pause
