@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "KEY=%USERPROFILE%\.ssh\id_ed25519_yandex"
set "HOST=enhel-method@158.160.169.204"
set "OUT=vm_snapshot.txt"

echo Running VM snapshot, this may take some time...
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_vm_snapshot.ps1" -KeyPath "%KEY%" -HostUser "%HOST%" -OutputFile "%OUT%"

echo.
echo Snapshot saved to %OUT%
pause

