@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: Окно с тестами: результаты по блокам (Telegram, промпт, Robokassa, UI). Окно остаётся открытым.
start "Тесты бота (блоки: Telegram, DeepSeek, Robokassa, UI)" cmd /k "cd /d "%~dp0" && python tests_bot.py"

:: Окно PowerShell 7 (локальные команды). По окончании откроет окно ВМ.
start "Локально — коммит и push" "C:\Program Files\PowerShell\7-preview\pwsh.exe" -NoExit -ExecutionPolicy Bypass -File "%~dp0deploy_local.ps1"
