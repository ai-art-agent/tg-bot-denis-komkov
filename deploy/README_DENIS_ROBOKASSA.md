# Мини-приложение Дениса: оплата через Робокассу Дениса

Чтобы кнопки «Оплатить» в мини-приложении вели на **Робокассу Дениса** (а не Enhel Method), мини-приложение и эндпоинт `/miniapp/create_order` должны работать из репозитория **tg-ai-denis-komkov** с его `.env` (логин/пароли Робокассы Дениса).

## Вариант: отдельный сервис на порту 8001

1. **Сервис Робокассы Дениса** — поднимает `robokassa_server` из проекта Дениса на порту **8001**:
   - `sudo cp deploy/robokassa-server-denis.service /etc/systemd/system/`
   - В сервисе проверьте пути (User, WorkingDirectory, EnvironmentFile, venv) под вашу ВМ.
   - `sudo systemctl daemon-reload && sudo systemctl enable robokassa-server-denis && sudo systemctl start robokassa-server-denis`

2. **Туннель** — cloudflared должен смотреть на **8001**:
   - В `run_cloudflared_quick.sh` по умолчанию используется `PORT=8001`.
   - В `cloudflared-quick.service` зависимость: `After=robokassa-server-denis.service`.

3. После перезапуска cloudflared мини-приложение будет открываться по тому же URL, но запросы пойдут на порт 8001 — на сервер Дениса с его Робокассой.

## Если на ВМ другой пользователь/путь

Отредактируйте `robokassa-server-denis.service`: замените `User=`, `WorkingDirectory=`, `EnvironmentFile=`, пути в `Environment=PATH` и `ExecStart=` на актуальные для вашего сервера (каталог tg-ai-denis-komkov и его venv).
