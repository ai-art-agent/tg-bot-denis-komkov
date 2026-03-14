#!/bin/bash
# Запускает cloudflared quick tunnel, парсит URL из вывода и пишет в MINIAPP_URL_FILE.
# Использование: MINIAPP_URL_FILE=/path/to/miniapp_url.txt ./run_cloudflared_quick.sh
# Или: ./run_cloudflared_quick.sh /path/to/miniapp_url.txt

OUT_FILE="${MINIAPP_URL_FILE:-${1:-./miniapp_url.txt}}"

cloudflared tunnel --url http://localhost:8000 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"
    if printf '%s\n' "$line" | grep -qE 'https://[^[:space:]]+trycloudflare\.com'; then
        url="$(printf '%s\n' "$line" | grep -oE 'https://[^[:space:]]+trycloudflare\.com' | head -1)"
        if [ -n "$url" ]; then
            printf '%s/miniapp\n' "$url" > "$OUT_FILE"
        fi
    fi
done
