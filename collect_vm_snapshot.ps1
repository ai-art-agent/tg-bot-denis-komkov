Param(
    [string]$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519_yandex",
    [string]$HostUser = "enhel-method@158.160.169.204",
    [string]$OutputFile = "vm_snapshot.txt"
)

$ErrorActionPreference = "Stop"

Write-Host "Собираю снимок состояния ВМ в файл $OutputFile ..." -ForegroundColor Cyan

$remoteScript = @'
SNAP="${HOME}/vm_snapshot_tmp_$(date +%Y%m%d_%H%M%S).txt"

echo "=== VM SNAPSHOT START ===" > "$SNAP"
echo "DATE: $(date -Iseconds)" >> "$SNAP"
echo "" >> "$SNAP"

echo "=== ROOT / (top-level) ===" >> "$SNAP"
ls -la / 2>&1 >> "$SNAP"
echo "" >> "$SNAP"

echo "=== HOME DIRECTORY ($HOME) ===" >> "$SNAP"
ls -la "$HOME" 2>&1 >> "$SNAP"
echo "" >> "$SNAP"
echo "--- HOME: directories one level deep ---" >> "$SNAP"
for d in "$HOME"/*/; do
  [ -d "$d" ] && echo "$d" >> "$SNAP" && ls -la "$d" 2>&1 | head -50 >> "$SNAP"
done
echo "" >> "$SNAP"

echo "=== /etc/systemd/system (unit files) ===" >> "$SNAP"
ls -la /etc/systemd/system/*.service 2>&1 >> "$SNAP"
echo "" >> "$SNAP"

for DIR in "$HOME/tg-ai-denis-komkov" "$HOME/tg-ai-enhel-method"; do
  if [ -d "$DIR" ]; then
    echo "=== PROJECT: $DIR ===" >> "$SNAP"
    cd "$DIR" || continue
    echo "--- GIT ---" >> "$SNAP"
    (git rev-parse HEAD 2>/dev/null || echo "no git") >> "$SNAP"
    echo "" >> "$SNAP"

    echo "--- TREE (max depth 3) ---" >> "$SNAP"
    find . -maxdepth 3 -type d | sort >> "$SNAP"
    echo "" >> "$SNAP"

    echo "--- FILE LIST (key files) ---" >> "$SNAP"
    find . -maxdepth 4 -type f \( -name "*.py" -o -name "*.service" -o -name "*.sh" -o -name "*.ps1" -o -name ".env" -o -name "MINIAPP*.md" \) | sort >> "$SNAP"
    echo "" >> "$SNAP"

    echo "--- FILE CONTENTS ---" >> "$SNAP"
    while IFS= read -r f; do
      [ -f "$f" ] || continue
      echo "----- FILE: $DIR/$f -----" >> "$SNAP"
      cat "$f" >> "$SNAP"
      echo "" >> "$SNAP"
    done < <(find . -maxdepth 4 -type f \( -name "*.py" -o -name "*.service" -o -name "*.sh" -o -name "*.ps1" -o -name ".env" -o -name "MINIAPP*.md" \) | sort)
    echo "" >> "$SNAP"
  fi
done

echo "=== SYSTEMD SERVICES ===" >> "$SNAP"
for SVC in tg-ai-denis-komkov robokassa-server robokassa-server-denis cloudflared-quick; do
  echo "----- systemctl status $SVC -----" >> "$SNAP"
  (systemctl status "$SVC" --no-pager 2>&1 || echo "service $SVC not found") >> "$SNAP"
  echo "" >> "$SNAP"
done

echo "=== VM SNAPSHOT END ===" >> "$SNAP"

cat "$SNAP"
'@

$sshArgs = @(
    "-i", $KeyPath,
    $HostUser,
    $remoteScript
)

$content = & ssh @sshArgs
Set-Content -Path $OutputFile -Value $content -Encoding UTF8

Write-Host "Готово. Снимок сохранён в $OutputFile" -ForegroundColor Green

