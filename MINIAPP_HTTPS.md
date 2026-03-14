# Мини-приложение по HTTPS (Cloudflare Tunnel)

Чтобы кнопка «Хочу продолжить» открывала мини-приложение **внутри Telegram** (а не в браузере), нужен **HTTPS**. Cloudflare Tunnel даёт бесплатный HTTPS-URL без своего домена.

---

## Вариант 1: Быстрый тест (без аккаунта, URL меняется)

Подходит только для проверки: после каждого перезапуска туннеля URL будет другой.

**На ВМ (по SSH):**

```bash
# Установить cloudflared (один раз)
sudo mkdir -p /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

# Запустить туннель (сервис на 8000 должен уже работать)
cloudflared tunnel --url http://localhost:8000
```

В консоли появится строка вида:

```
https://xxxx-xx-xx-xx-xx.trycloudflare.com
```

Скопируйте её и в `.env` на ВМ укажите:

```env
MINIAPP_URL=https://xxxx-xx-xx-xx-xx.trycloudflare.com/miniapp
```

Перезапустите бота: `sudo systemctl restart tg-ai-denis-komkov`.  
Туннель должен оставаться запущенным (не закрывайте SSH или запустите в screen/tmux). При следующем запуске `cloudflared tunnel --url ...` URL изменится — для постоянной работы используйте вариант 2.

---

## Вариант 2: Стабильный URL (аккаунт Cloudflare, бесплатно) — подробно

URL будет постоянным, туннель работает как сервис и переживёт перезагрузку ВМ.

---

### Шаг 1: Регистрация и вход в Cloudflare

1. Откройте в браузере: **https://dash.cloudflare.com/sign-up**
2. Введите email и пароль, нажмите **Create Account**. Подтвердите email, если попросят.
3. Войдите в аккаунт: **https://dash.cloudflare.com/login**

---

### Шаг 2: Первый вход в Zero Trust (один раз)

1. В левом меню дашборда Cloudflare нажмите **Zero Trust** (или откройте **https://one.dash.cloudflare.com**).
2. Если первый раз: появится экран «Welcome to Zero Trust». Нажмите **Get started** или **Continue**.
3. Введите **Team name** (например `miniapp-team`) — это часть URL панели, не домен. Нажмите **Continue**.
4. Выберите тариф **Free** (0$) и нажмите **Continue**. Дальше можно закрыть приглашение коллег (Skip).

После этого вы окажетесь в панели Zero Trust (меню слева: Access, Gateway, Networks и т.д.).

---

### Шаг 3: Домен для туннеля (если ещё нет)

Туннелю нужен «домен» в Cloudflare, чтобы выдать вам адрес вида `https://поддомен.домен`.

**Вариант А — уже есть свой домен:**

1. В основном дашборде: **Websites** → **Add a site**.
2. Введите домен (например `mydomain.com`), нажмите **Add site**.
3. Выберите план **Free**, нажмите **Continue**.
4. Cloudflare покажет две NS-записи (типа `ada.ns.cloudflare.com` и т.п.). В панели регистратора домена замените NS вашего домена на эти два значения. После сохранения в Cloudflare нажмите **Done, check nameservers** (проверка может занять до нескольких часов).

**Вариант Б — нет домена (бесплатный поддомен):**

1. Зайдите на **https://www.freenom.com** (или другой сервис бесплатных доменов) и зарегистрируйте бесплатный домен (например `mybot.tk` или `mybot.ml`).
2. Добавьте этот домен в Cloudflare как в варианте А (Add a site → ввести домен → заменить NS у регистратора на те, что даст Cloudflare).

**Вариант В — только проверить туннель без домена:**

Можно временно использовать **Вариант 1** (quick tunnel) из этой инструкции. Для постоянного стабильного URL нужен хотя бы один домен в Cloudflare (А или Б).

---

### Шаг 4: Создание туннеля в Zero Trust

1. В Zero Trust в левом меню откройте **Networks** → **Tunnels** (или **Connections** → **Tunnels** в новых версиях).
2. Нажмите кнопку **Create a tunnel** (или **Add a tunnel**).
3. Тип туннеля: выберите **Cloudflared** и нажмите **Next**.
4. В поле **Tunnel name** введите, например: `miniapp` (латиница, без пробелов). Нажмите **Save tunnel**.

Появится экран настройки туннеля с разделами: **Public Hostname**, **Private Network** (можно не трогать), **Connectors**.

---

### Шаг 5: Публичный хостнейм (HTTPS-адрес)

1. В карточке туннеля найдите блок **Public Hostname** и нажмите **Add a public hostname** (или **+ Public hostname**).
2. Заполните форму:
   - **Subdomain:** введите поддомен, например `denis-miniapp` или `tg-miniapp`. Итоговый адрес будет `https://denis-miniapp.ваш-домен.com`.
   - **Domain:** в выпадающем списке выберите ваш домен (тот, что добавлен в Cloudflare на шаге 3). Если списка нет — сначала добавьте домен (шаг 3).
   - **Service type:** оставьте **HTTP**.
   - **URL** (куда слать трафик): введите `localhost:8000` (без `http://` — только хост и порт).
3. Нажмите **Save hostname**.

Запомните или запишите итоговый адрес: **https://ваш-поддомен.ваш-домен.com** (именно он понадобится для `MINIAPP_URL`).

---

### Шаг 6: Установка connector на ВМ

1. На той же странице туннеля найдите раздел **Connectors** (или вкладку **Install connector**).
2. Нажмите **Install connector** (или **Next** до шага установки).
3. В списке ОС выберите **Linux** (иконка пингвина).
4. В блоке появится одна длинная команда вида:
   ```bash
   sudo cloudflared service install eyJhIjoi...
   ```
   Скопируйте её **целиком** (включая `sudo` и длинный токен).
5. Подключитесь к своей ВМ по SSH (как обычно при деплое), например:
   ```bash
   ssh -i "C:\Users\AI_Art\.ssh\id_ed25519_yandex" enhel-method@158.160.169.204
   ```
6. На ВМ сначала установите `cloudflared`, если ещё не ставили (один раз):
   ```bash
   sudo mkdir -p /usr/share/keyrings
   curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null
   echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update && sudo apt-get install -y cloudflared
   ```
7. Вставьте скопированную команду установки connector (из п. 4) и выполните её. Должно появиться сообщение об успешной установке сервиса.
8. Проверьте и включите автозапуск:
   ```bash
   sudo systemctl status cloudflared
   sudo systemctl enable cloudflared
   ```
   В статусе должно быть `active (running)`. Если не запустился: `sudo systemctl start cloudflared`, затем снова `status`.

---

### Шаг 7: MINIAPP_URL на ВМ и перезапуск бота

1. На ВМ откройте файл `.env` в каталоге проекта бота (например `/home/enhel-method/tg-ai-denis-komkov/.env` или как у вас):
   ```bash
   nano /home/enhel-method/tg-ai-denis-komkov/.env
   ```
2. Найдите строку **MINIAPP_URL** и задайте полный URL до мини-приложения (тот, что получился на шаге 5, **плюс путь `/miniapp`**):
   ```env
   MINIAPP_URL=https://denis-miniapp.ваш-домен.com/miniapp
   ```
   Пример: если поддомен `tg-miniapp`, домен `mybot.tk`, то:
   ```env
   MINIAPP_URL=https://tg-miniapp.mybot.tk/miniapp
   ```
   Сохраните файл (в nano: Ctrl+O, Enter, Ctrl+X).
3. Перезапустите бота:
   ```bash
   sudo systemctl restart tg-ai-denis-komkov
   ```

---

### Шаг 8: Проверка

1. В браузере откройте: **https://ваш-поддомен.ваш-домен.com/miniapp**  
   Должна открыться страница выбора формата (карточки: групповые занятия, вебинар, Pro, личная работа). Если страница не открывается — проверьте, что на ВМ запущен `robokassa-server` (`sudo systemctl status robokassa-server`) и что туннель активен (`sudo systemctl status cloudflared`).
2. В Telegram откройте бота, дойдите до шага с кнопкой **«Хочу продолжить»** и нажмите её. Должно открыться **окно внутри Telegram** с той же страницей выбора формата (не во внешнем браузере).

Готово: мини-приложение работает по стабильному HTTPS и открывается внутри Telegram.

---

## Проверка

1. **Сервис на 8000:** на ВМ должен быть запущен `robokassa-server` (или ваш сервер с эндпоинтом `/miniapp`):

   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/miniapp
   ```
   Ожидается `200`.

2. **Туннель:** при варианте 2 — `sudo systemctl status cloudflared` в состоянии `active (running)`.

3. **HTTPS:** в браузере открыть `https://ваш-хостнейм/miniapp` — без ошибок сертификата, страница мини-приложения загружается.

4. **Telegram:** в боте дойти до «Хочу продолжить» и нажать кнопку — должно открыться окно Web App внутри Telegram.

---

---

## Вариант 2б: Туннель через CLI (config.yml)

Если вы создаёте туннель командами (нужен домен в Cloudflare и `cloudflared tunnel login` с ВМ или скопированный cert).

**На ВМ:**

```bash
# Установка cloudflared — см. вариант 1

# Вход (откроется ссылка для браузера — выполните на ВМ с консолью или скопируйте cert на машину с браузером)
cloudflared tunnel login

# Создать туннель
cloudflared tunnel create miniapp
# Запомните Tunnel ID из вывода и путь к credentials-file (например /home/USER/.cloudflared/UUID.json)

# Маршрут DNS (hostname — поддомен вашего домена в Cloudflare)
cloudflared tunnel route dns miniapp miniapp.ваш-домен.com

# Создать конфиг
mkdir -p ~/.cloudflared
nano ~/.cloudflared/config.yml
```

Содержимое `~/.cloudflared/config.yml`:

```yaml
url: http://localhost:8000
tunnel: <Tunnel-UUID>
credentials-file: /home/USER/.cloudflared/<Tunnel-UUID>.json
```

Подставьте свой `Tunnel-UUID` и путь к `credentials-file` из вывода `tunnel create`.

Установить и запустить как сервис:

```bash
sudo cloudflared --config /home/USER/.cloudflared/config.yml service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

В `.env`: `MINIAPP_URL=https://miniapp.ваш-домен.com/miniapp`

---

## Если туннель без своего домена

Для разового теста используйте **вариант 1** (quick tunnel). Для стабильного URL без своего домена проще всего **вариант 2** (Zero Trust): при создании туннеля в панели иногда доступен бесплатный домен в списке **Domain** — выберите его и задайте свой Subdomain.
