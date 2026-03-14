# Развёртывание авто-мини-приложения (quick tunnel на ВМ)

Бот читает URL мини-приложения из файла `miniapp_url.txt`. Скрипт на ВМ запускает cloudflared quick tunnel, парсит новый URL и пишет его в этот файл. При отсутствии URL бот показывает «Что-то пошло не так. Попробуйте ещё раз.» с кнопкой «Попробовать ещё раз».

---

## Что уже сделано в коде

- **bot.py**: чтение базового URL из `miniapp_url.txt` (рядом с bot.py), при пустом файле — из `.env`; при отсутствии URL — сообщение «Что-то пошло не так. Попробуйте ещё раз.» и кнопка «Попробовать ещё раз» (повторная попытка открыть мини-приложение).
- **deploy/run_cloudflared_quick.sh**: скрипт, запускающий `cloudflared tunnel --url http://localhost:8000` и записывающий `https://...trycloudflare.com/miniapp` в файл.
- **deploy/cloudflared-quick.service**: systemd-юнит для автозапуска туннеля с рестартом при падении.

---

## Пошаговые действия на ВМ

### 1. Обновить код на ВМ

На своей машине выполните деплой (чтобы на ВМ попали изменения из репозитория):

- Запустите `deploy_to_vm.bat` (или вручную: git push, затем на ВМ `git pull` в каталоге проекта бота).

На ВМ по SSH:

```bash
cd /home/enhel-method/tg-ai-denis-komkov
git pull
```

(Если проект бота у вас в другом каталоге — подставьте его, далее везде используйте свой путь.)

---

### 2. Убедиться, что cloudflared установлен

На ВМ:

```bash
which cloudflared
```

Если команда не найдена, установите (один раз):

```bash
sudo mkdir -p /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

---

### 3. Сделать скрипт исполняемым

На ВМ (подставьте свой путь к проекту, если не `tg-ai-denis-komkov`):

```bash
chmod +x /home/enhel-method/tg-ai-denis-komkov/deploy/run_cloudflared_quick.sh
```

---

### 4. Подставить путь в systemd-юнит (если проект не в tg-ai-denis-komkov)

Если проект бота лежит не в `/home/enhel-method/tg-ai-denis-komkov`, отредактируйте юнит:

```bash
sudo nano /etc/systemd/system/cloudflared-quick.service
```

Замените все вхождения `/home/enhel-method/tg-ai-denis-komkov` на ваш путь (например `/home/enhel-method/tg-ai-enhel-method`). Сохраните (Ctrl+O, Enter, Ctrl+X).

---

### 5. Установить и запустить сервис cloudflared-quick

На ВМ:

```bash
sudo cp /home/enhel-method/tg-ai-denis-komkov/deploy/cloudflared-quick.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cloudflared-quick
sudo systemctl start cloudflared-quick
sudo systemctl status cloudflared-quick --no-pager
```

Статус должен быть `active (running)`. Подождите 5–10 секунд, чтобы cloudflared успел вывести URL.

---

### 6. Проверить, что URL записался в файл

На ВМ:

```bash
cat /home/enhel-method/tg-ai-denis-komkov/miniapp_url.txt
```

Должна быть одна строка вида: `https://xxxx.trycloudflare.com/miniapp`. Если файл пустой — посмотрите логи:

```bash
sudo journalctl -u cloudflared-quick.service -n 30 --no-pager
```

В логе должна быть строка `Your quick Tunnel has been created!` и URL. Если её нет — проверьте, что `robokassa-server` запущен (`curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/miniapp` возвращает 200).

---

### 7. Убрать MINIAPP_URL из .env (по желанию)

Чтобы бот использовал только файл, можно закомментировать или удалить строку `MINIAPP_URL=...` в `.env` на ВМ. Если оставить — файл имеет приоритет, при пустом файле подставится значение из .env.

```bash
nano /home/enhel-method/tg-ai-denis-komkov/.env
```

Строку `MINIAPP_URL=...` можно оставить пустой или закомментировать: `# MINIAPP_URL=`.

---

### 8. Перезапустить бота

На ВМ:

```bash
sudo systemctl restart tg-ai-denis-komkov
sudo systemctl status tg-ai-denis-komkov --no-pager
```

---

### 9. Проверка в Telegram

1. Дойдите в боте до шага «Хочу продолжить» и нажмите кнопку — должно открыться мини-приложение (окно внутри Telegram).
2. Если вместо этого появится «Что-то пошло не так. Попробуйте ещё раз.» — нажмите «Попробовать ещё раз». После того как туннель запишет URL в файл, при следующем нажатии мини-приложение откроется.

---

## Авто-рестарт при сбоях

- **cloudflared-quick**: при падении процесса systemd перезапустит сервис через 5 секунд (`RestartSec=5`). Новый запуск сгенерирует новый URL и перезапишет `miniapp_url.txt`.
- Бот при каждом нажатии «Хочу продолжить» или «Попробовать ещё раз» заново читает `miniapp_url.txt`, поэтому после восстановления туннеля всё заработает без перезапуска бота.

---

## Если проект на ВМ в другом каталоге

Замените во всех командах и в юните путь `/home/enhel-method/tg-ai-denis-komkov` на ваш (например `/home/enhel-method/tg-ai-enhel-method`). Важно, чтобы:

- `miniapp_url.txt` лежал в **том же каталоге, откуда запускается бот** (рядом с `bot.py`);
- в `cloudflared-quick.service` переменная `MINIAPP_URL_FILE` указывала на этот же файл;
- `ExecStart` вызывал скрипт из вашего репозитория (например `.../tg-ai-enhel-method/deploy/run_cloudflared_quick.sh`).
