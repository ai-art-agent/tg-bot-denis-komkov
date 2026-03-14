# Remote: SSH to VM, pull and restart (window stays open)
$ErrorActionPreference = "Continue"
$keyPath = Join-Path $env:USERPROFILE ".ssh\id_ed25519_yandex"
$hostUser = "enhel-method@158.160.169.204"
# Одна строка — иначе под Windows CRLF ломает bash (cd: too many arguments, syntax error near `&&').
$remoteCmd = "cd ~/tg-ai-denis-komkov && git pull && venv/bin/pip install -r requirements.txt && sudo cp deploy/tg-ai-denis-komkov.service /etc/systemd/system/ 2>/dev/null; sudo cp deploy/robokassa-server.service /etc/systemd/system/ 2>/dev/null; sudo cp deploy/robokassa-server-denis.service /etc/systemd/system/ 2>/dev/null; sudo cp deploy/cloudflared-quick.service /etc/systemd/system/ 2>/dev/null; sudo systemctl daemon-reload && sudo systemctl restart tg-ai-denis-komkov && (sudo systemctl restart robokassa-server 2>/dev/null || true) && (sudo systemctl restart robokassa-server-denis 2>/dev/null || true) && (sudo systemctl restart cloudflared-quick 2>/dev/null || true) && echo '' && echo '--- Service status (bot Denis) ---' && sudo systemctl status tg-ai-denis-komkov --no-pager && echo '' && echo '--- Robokassa (Enhel) ---' && (sudo systemctl status robokassa-server --no-pager 2>/dev/null || echo 'not configured') && echo '' && echo '--- Robokassa (Denis) ---' && (sudo systemctl status robokassa-server-denis --no-pager 2>/dev/null || echo 'not configured') && echo '' && echo '--- Cloudflared quick ---' && (sudo systemctl status cloudflared-quick --no-pager 2>/dev/null || echo 'not configured')"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Window 2: VM - git pull and restart" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

& ssh -i $keyPath $hostUser $remoteCmd

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Done. Press Enter to close." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Read-Host "Press Enter to close"
