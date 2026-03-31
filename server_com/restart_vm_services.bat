@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."
cd /d "%ROOT_DIR%"

rem ===== Настройки подключения к ВМ =====
set "KEY=%USERPROFILE%\.ssh\id_ed25519_yandex"
set "HOST=enhel-method@158.160.169.204"

rem ===== Каталог проекта на ВМ =====
set "REMOTE_PROJECT_DIR=~/tg-ai-denis-komkov"

rem ===== Имена служб systemd на ВМ (только стек Дениса; Энхель — robokassa-server — не трогаем) =====
set "BOT_SERVICE=tg-ai-denis-komkov"
set "ROBOKASSA_SERVICE=robokassa-server-denis"
rem Сначала quick-туннель (как у вас на ВМ), иначе unit cloudflared
set "TUNNEL_SERVICE=cloudflared-quick"
set "TUNNEL_SERVICE_ALT=cloudflared"

echo Перезапуск служб на ВМ %HOST%...
echo.

ssh -i "%KEY%" "%HOST%" "bash -lc 'set -e; cd %REMOTE_PROJECT_DIR%; SERVICES=\"%BOT_SERVICE% %ROBOKASSA_SERVICE%\"; if systemctl status %TUNNEL_SERVICE% >/dev/null 2>&1; then SERVICES=\"$SERVICES %TUNNEL_SERVICE%\"; elif systemctl status %TUNNEL_SERVICE_ALT% >/dev/null 2>&1; then SERVICES=\"$SERVICES %TUNNEL_SERVICE_ALT%\"; fi; echo \"Remote dir: %REMOTE_PROJECT_DIR%\"; echo \"Restarting: $SERVICES\"; sudo systemctl restart $SERVICES; echo \"=== STATUS ===\"; for s in $SERVICES; do sudo systemctl --no-pager --full status \"$s\" | sed -n \"1,25p\"; echo; done'"
if errorlevel 1 (
    echo.
    echo Ошибка перезапуска. Проверьте:
    echo - доступ по SSH: %HOST%
    echo - ключ: %KEY%
    echo - корректность имён служб в этом .bat
    echo - права sudo у пользователя на ВМ
    pause
    exit /b 1
)

echo.
echo Готово: службы перезапущены.
pause
