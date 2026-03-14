from __future__ import annotations

"""
HTTP-сервер Robokassa для развёртывания на ВМ (Yandex Compute Cloud).

Эндпоинты:
  GET/POST /robokassa/result — ResultURL (server-to-server); метод задаётся в настройках магазина Робокассы. Возвращает "OK{InvId}" или "ERROR"
  GET  /robokassa/success — SuccessURL (редирект после оплаты)
  GET  /robokassa/fail    — FailURL (отмена/ошибка оплаты)

Запуск (в venv на ВМ, пример):
  uvicorn robokassa_server:app --host 0.0.0.0 --port 8000 --http h11

  Параметр --http h11 использует парсер h11 вместо httptools; если в логах
  есть «Invalid HTTP request received» при вызовах от Робокассы, h11 часто
  принимает такие запросы и тогда в логе появится строка GET /robokassa/result.

В кабинете Robokassa:
  Result URL  = http://ВАШ_IP:8000/robokassa/result
  Success URL = http://ВАШ_IP:8000/robokassa/success
  Fail URL    = http://ВАШ_IP:8000/robokassa/fail

На ВМ в .env задайте TELEGRAM_BOT_USERNAME (без @), например MyPsychologistBot,
чтобы на страницах успеха/ошибки показывалась кнопка «Открыть чат» и на мобильных
выполнялся переход в приложение Telegram.

Документация: https://docs.robokassa.ru/ru/notifications-and-redirects
При фильтрации по IP разрешите: 185.59.216.65, 185.59.217.65

Тестовый режим (IsTest=1): на тестовой странице оплаты выберите блок
«Успешное проведение платежа» — иначе попадёте на Fail URL и ResultURL не вызовется.
"""

import os
import time
import json
import logging
import logging.handlers
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, HTMLResponse

from robokassa_integration import (
    PaymentsDB,
    RobokassaConfig,
    verify_result_url,
    verify_success_url,
    process_result_url,
    build_payment_url,
    _to_amount_str,
)

logger = logging.getLogger("robokassa_server")
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Ротация лога оплат (ВМ): robokassa.log, до 5 MB, 2 резервные копии (см. LOGGING.md).
_robokassa_handler = logging.handlers.RotatingFileHandler(
    "robokassa.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=2,
    encoding="utf-8",
)
_robokassa_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_robokassa_handler)
logger.setLevel(logging.INFO)

app = FastAPI()


def _amount_from_env(name: str, default: str) -> str:
    v = os.getenv(name, default)
    try:
        return _to_amount_str(v)
    except Exception:
        return _to_amount_str(default)


# Цены из .env (аналогично bot.py)
PRICE_GROUP_STANDARD_RUB = _amount_from_env("PRICE_GROUP_STANDARD_RUB", os.getenv("PRICE_GROUP_RUB", "24990"))
PRICE_GROUP_VIP_RUB = _amount_from_env("PRICE_GROUP_VIP_RUB", os.getenv("PRICE_GROUP_RUB", "45990"))
PRICE_WEBINAR_RUB = _amount_from_env("PRICE_WEBINAR_RUB", "2990")
PRICE_PRO_RUB = _amount_from_env("PRICE_PRO_RUB", "990")
PRICE_PERSONAL_1M_RUB = _amount_from_env("PRICE_PERSONAL_1M_RUB", "120000")
PRICE_PERSONAL_2M_RUB = _amount_from_env("PRICE_PERSONAL_2M_RUB", "180000")
PRICE_PERSONAL_4M_RUB = _amount_from_env("PRICE_PERSONAL_4M_RUB", "300000")

PRODUCTS = {
    "group_standard": {
        "amount": PRICE_GROUP_STANDARD_RUB,
        "description": "Оплата: Групповые занятия (Стандарт)",
    },
    "group_vip": {
        "amount": PRICE_GROUP_VIP_RUB,
        "description": "Оплата: Групповые занятия (VIP)",
    },
    "webinar": {
        "amount": PRICE_WEBINAR_RUB,
        "description": "Оплата: Онлайн вебинар",
    },
    "pro": {
        "amount": PRICE_PRO_RUB,
        "description": "Оплата: AI-Психолог Pro (месяц, предзаказ)",
    },
    "personal_1m": {
        "amount": PRICE_PERSONAL_1M_RUB,
        "description": "Оплата: Личная работа 1 месяц",
    },
    "personal_2m": {
        "amount": PRICE_PERSONAL_2M_RUB,
        "description": "Оплата: Личная работа 2 месяца",
    },
    "personal_4m": {
        "amount": PRICE_PERSONAL_4M_RUB,
        "description": "Оплата: Личная работа 4 месяца",
    },
}


@app.middleware("http")
async def log_robokassa_requests(request: Request, call_next):
    """Логирует каждый запрос к /robokassa/* с IP — чтобы видеть вызовы от Робокассы (185.59.216.65)."""
    if request.url.path.startswith("/robokassa/"):
        client = request.client
        host = client.host if client else request.headers.get("x-forwarded-for", "?")
        logger.info("Robokassa request: %s %s from %s", request.method, request.url.path, host)
    return await call_next(request)


async def _collect_params(request: Request) -> Dict[str, Any]:
    """
    Собираем параметры из query string и form-urlencoded body.
    Robokassa может отправлять данные и так, и так.
    """
    params: Dict[str, Any] = {}
    for k, v in request.query_params.multi_items():
        params[k] = v
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            form = await request.form()
            for k, v in form.multi_items():
                params[k] = v
        except Exception:
            # не form-data — можно игнорировать
            pass
    return params


def _bot_open_link() -> str:
    """Ссылка для открытия чата (t.me/username). Пустая строка, если username не задан."""
    username = (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if not username:
        return ""
    return f"https://t.me/{username}"


def _success_html() -> str:
    link = _bot_open_link()
    if link:
        # username для tg://resolve?domain=... (на мобильных открывает приложение)
        username = link.rstrip("/").split("/")[-1] if link else ""
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Оплата принята</title>
  <style>
    body {{ font-family: system-ui, sans-serif; text-align: center; padding: 2rem; margin: 0; }}
    .msg {{ margin-bottom: 1.5rem; color: #333; }}
    a.btn {{ display: inline-block; padding: 12px 24px; background: #0088cc; color: #fff; text-decoration: none; border-radius: 8px; font-size: 1.1rem; }}
    a.btn:hover {{ background: #006699; }}
  </style>
  <script>
    (function() {{
      var username = {json.dumps(username)};
      var isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
      if (isMobile && username) {{
        setTimeout(function() {{ window.location.href = 'tg://resolve?domain=' + username; }}, 1500);
      }}
    }})();
  </script>
</head>
<body>
  <p class="msg">Оплата принята. Ваш доступ уже отправлен — откройте чат, чтобы получить его.</p>
  <p><a class="btn" href="{link}">Открыть чат</a></p>
</body>
</html>"""
    return """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Оплата принята</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:2rem"><p>Оплата принята. Ваш доступ уже отправлен.</p></body>
</html>"""


def _fail_html() -> str:
    link = _bot_open_link()
    if link:
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Оплата не завершена</title>
  <style>
    body {{ font-family: system-ui, sans-serif; text-align: center; padding: 2rem; margin: 0; }}
    .msg {{ margin-bottom: 1.5rem; color: #333; }}
    a.btn {{ display: inline-block; padding: 12px 24px; background: #0088cc; color: #fff; text-decoration: none; border-radius: 8px; font-size: 1.1rem; }}
    a.btn:hover {{ background: #006699; }}
  </style>
</head>
<body>
  <p class="msg">Оплата не завершена. Вы можете попробовать ещё раз.</p>
  <p><a class="btn" href="{link}">Вернуться в чат</a></p>
</body>
</html>"""
    return """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Оплата не завершена</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:2rem"><p>Оплата не завершена. Вы можете попробовать ещё раз.</p></body>
</html>"""


def _miniapp_html() -> str:
    """Простое мини-приложение Telegram WebApp: выбор формата, продуктов и переход к оплате."""
    # Цены для отображения (с пробелами в разрядах)
    def fmt(v: str) -> str:
        try:
            n = int(float(v.replace(",", ".").replace(" ", "")))
            return f"{n:,}".replace(",", " ")
        except Exception:
            return v

    price_group_std = fmt(PRICE_GROUP_STANDARD_RUB)
    price_group_std_open = "29 990"
    price_group_vip = fmt(PRICE_GROUP_VIP_RUB)
    price_group_vip_open = "54 990"
    price_webinar = fmt(PRICE_WEBINAR_RUB)
    price_pro_today = fmt(PRICE_PRO_RUB)
    price_pro_open = "1 990"
    price_p1 = fmt(PRICE_PERSONAL_1M_RUB)
    price_p2 = fmt(PRICE_PERSONAL_2M_RUB)
    price_p4 = fmt(PRICE_PERSONAL_4M_RUB)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Выбор формата</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{
      --bg: #0b1220;
      --card-bg: #111827;
      --accent: #22c55e;
      --accent-soft: rgba(34, 197, 94, 0.16);
      --text: #e5e7eb;
      --muted: #9ca3af;
      --danger: #f97373;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0b1220;
      color: var(--text);
    }}
    body {{
      padding: 12px 16px 24px;
    }}
    .container {{
      max-width: 480px;
      margin: 0 auto;
      min-height: 100%;
    }}
    .nav-back {{
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 12px;
      padding: 6px 0;
      cursor: pointer;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .nav-back:hover {{ color: var(--text); }}
    .nav-back-arrow {{ font-size: 1.2rem; }}
    h1 {{ font-size: 1.2rem; margin: 0 0 6px; }}
    .lead {{ font-size: 0.85rem; color: var(--muted); margin-bottom: 10px; line-height: 1.35; }}
    .cards {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 10px;
    }}
    .cards.single-col {{
      grid-template-columns: 1fr;
    }}
    .card {{
      background: linear-gradient(145deg, rgba(15,23,42,0.95), rgba(15,23,42,0.9));
      border-radius: 12px;
      padding: 10px 10px 8px;
      border: 1px solid rgba(148,163,184,0.25);
      box-shadow: 0 8px 24px rgba(15,23,42,0.9);
      cursor: pointer;
      transition: transform 0.12s ease-out, box-shadow 0.12s ease-out, border-color 0.12s;
    }}
    .card.selected {{
      border-color: rgba(34,197,94,0.9);
      box-shadow: 0 0 0 2px rgba(34,197,94,0.35);
    }}
    .card:hover {{
      transform: translateY(-1px);
      border-color: rgba(34,197,94,0.7);
      box-shadow: 0 22px 45px rgba(15,23,42,0.9);
    }}
    .card-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 6px;
      margin-bottom: 4px;
    }}
    .card-title {{
      font-size: 0.82rem;
      font-weight: 600;
      line-height: 1.2;
    }}
    .badge {{
      font-size: 0.58rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      padding: 2px 6px;
      border-radius: 999px;
      background: rgba(15,23,42,0.9);
      border: 1px solid rgba(148,163,184,0.35);
      color: var(--muted);
      white-space: nowrap;
    }}
    .badge-accent {{
      border-color: rgba(34,197,94,0.6);
      background: rgba(22,163,74,0.08);
      color: #bbf7d0;
    }}
    .price-row {{
      display: flex;
      align-items: baseline;
      gap: 6px;
      margin-bottom: 2px;
    }}
    .price-main {{
      font-size: 0.88rem;
      font-weight: 600;
      color: #e5e7eb;
    }}
    .price-old {{
      font-size: 0.7rem;
      color: #6b7280;
      text-decoration: line-through;
    }}
    .card-desc {{
      font-size: 0.72rem;
      color: var(--muted);
      line-height: 1.3;
      margin: 0;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 6px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: #bbf7d0;
      font-size: 0.62rem;
      margin-top: 4px;
    }}
    .view {{
      display: none;
    }}
    .view.active {{
      display: block;
    }}
    .section-title {{ font-size: 1.1rem; margin: 0 0 4px; }}
    .section-sub {{ font-size: 0.86rem; color: var(--muted); margin-bottom: 10px; }}
    .pill-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 6px 0 10px;
    }}
    .pill-option {{
      padding: 5px 10px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.5);
      font-size: 0.78rem;
      cursor: pointer;
      color: var(--muted);
    }}
    .pill-option.active {{
      border-color: rgba(34,197,94,0.8);
      background: rgba(22,163,74,0.16);
      color: #bbf7d0;
    }}
    .btn-row {{
      display: flex;
      gap: 8px;
      margin-top: 14px;
    }}
    .btn {{
      flex: 1;
      border: none;
      border-radius: 999px;
      padding: 10px 14px;
      font-size: 0.92rem;
      font-weight: 500;
      cursor: pointer;
    }}
    .btn-primary {{
      background: linear-gradient(135deg, #22c55e, #16a34a);
      color: #022c22;
    }}
    .btn-secondary {{
      background: rgba(15,23,42,0.9);
      color: var(--muted);
      border: 1px solid rgba(148,163,184,0.5);
    }}
    .bullet-list {{
      margin: 6px 0 0;
      padding-left: 14px;
      font-size: 0.75rem;
      color: var(--muted);
    }}
    .bullet-list li {{
      margin-bottom: 2px;
    }}
    #view-personal .cards {{
      grid-template-columns: 1fr 1fr 1fr;
      gap: 8px;
    }}
    #view-personal .card {{
      padding: 8px;
    }}
    #view-personal .card-title {{ font-size: 0.75rem; }}
    #view-personal .price-main {{ font-size: 0.82rem; }}
  </style>
</head>
<body>
  <div class="container">
    <div id="view-intro" class="view active">
      <h1>Шаг к изменениям</h1>
      <p class="lead">
        Вы уже сделали шаг к изменениям. Выберите формат, который лучше всего поддержит вас в этом процессе.
      </p>
      <div class="cards">
        <div class="card" onclick="openProduct('group')">
          <div class="card-header">
            <div class="card-title">Групповые занятия</div>
            <span class="badge badge-accent">Глубинная работа 4 недели</span>
          </div>
          <div class="price-row">
            <span class="price-main">{price_group_std} ₽ · Стандарт</span>
            <span class="price-old">{price_group_std_open} ₽</span>
          </div>
          <p class="card-desc">
            Небольшая группа, живая поддержка Дениса и участников, пошаговое сопровождение.
          </p>
          <div class="pill">Есть VIP-формат с личными сессиями</div>
        </div>

        <div class="card" onclick="openProduct('webinar')">
          <div class="card-header">
            <div class="card-title">Онлайн вебинар</div>
            <span class="badge">Стартовый шаг</span>
          </div>
          <div class="price-row">
            <span class="price-main">{price_webinar} ₽</span>
          </div>
          <p class="card-desc">
            Интенсив до 1,5 часов для самостоятельной проработки важной темы.
          </p>
        </div>

        <div class="card" onclick="openProduct('pro')">
          <div class="card-header">
            <div class="card-title">AI‑Психолог Pro</div>
            <span class="badge badge-accent">Предзаказ</span>
          </div>
          <div class="price-row">
            <span class="price-main">{price_pro_today} ₽ / мес</span>
            <span class="price-old">{price_pro_open} ₽ / мес</span>
          </div>
          <p class="card-desc">
            Личный ИИ‑помощник на основе моих методик, доступный 24/7.
          </p>
          <div class="pill">Сейчас действует предзаказ по сниженной цене</div>
        </div>

        <div class="card" onclick="openProduct('personal')">
          <div class="card-header">
            <div class="card-title">Личная работа 1‑на‑1</div>
            <span class="badge">Глубокий индивидуальный формат</span>
          </div>
          <div class="price-row">
            <span class="price-main">от {price_p1} ₽</span>
          </div>
          <p class="card-desc">
            Индивидуальное сопровождение по ключевым запросам, сессии по 3–4 часа и плотный контакт.
          </p>
        </div>
      </div>
    </div>

    <div id="view-group" class="view">
      <div class="nav-back" onclick="showView('intro')"><span class="nav-back-arrow">←</span> Назад</div>
      <h2 class="section-title">Групповые занятия</h2>
      <p class="section-sub">4 недели глубинной работы в небольшой группе с моим сопровождением.</p>
      <div class="pill-row">
        <div id="pill-group-standard" class="pill-option active" onclick="selectGroupTariff('standard')">
          Стандарт · {price_group_std} ₽ <span class="price-old">{price_group_std_open} ₽</span>
        </div>
        <div id="pill-group-vip" class="pill-option" onclick="selectGroupTariff('vip')">
          VIP · {price_group_vip} ₽ <span class="price-old">{price_group_vip_open} ₽</span>
        </div>
      </div>
      <ul class="bullet-list">
        <li>Небольшая группа и безопасное пространство.</li>
        <li>Еженедельные Zoom‑встречи и домашние задания.</li>
        <li>Поддержка и обратная связь от меня по ходу пути.</li>
      </ul>
      <p class="card-desc" style="margin-top:8px;">
        В VIP‑формате дополнительно: 2 личные глубинные проработки 1‑на‑1 со мной (раз в 2 недели).
      </p>
      <div class="btn-row">
        <button class="btn btn-secondary" onclick="onThink('group')">Еще подумаю</button>
        <button class="btn btn-primary" onclick="onPayGroup()">Оплатить</button>
      </div>
    </div>

    <div id="view-webinar" class="view">
      <div class="nav-back" onclick="showView('intro')"><span class="nav-back-arrow">←</span> Назад</div>
      <h2 class="section-title">Онлайн вебинар</h2>
      <p class="section-sub">
        Интенсив до 1,5 часов для самостоятельной проработки состояния и понимания причин.
      </p>
      <ul class="bullet-list">
        <li>Запись, к которой можно возвращаться в своём темпе.</li>
        <li>Практики и упражнения, которые можно сразу применять.</li>
        <li>Хороший шаг, если пока не готов к глубокой работе.</li>
      </ul>
      <div class="price-row" style="margin-top:8px;">
        <span class="price-main">{price_webinar} ₽</span>
      </div>
      <div class="btn-row">
        <button class="btn btn-secondary" onclick="onThink('webinar')">Еще подумаю</button>
        <button class="btn btn-primary" onclick="onPay('webinar')">Оплатить</button>
      </div>
    </div>

    <div id="view-pro" class="view">
      <div class="nav-back" onclick="showView('intro')"><span class="nav-back-arrow">←</span> Назад</div>
      <h2 class="section-title">AI‑Психолог Pro</h2>
      <p class="section-sub">
        Ваш личный ИИ‑помощник на основе моих методик, доступный 24/7 для поддержки в мыслях и состояниях.
      </p>
      <ul class="bullet-list">
        <li>Предзаказ по специальной цене: {price_pro_today} ₽ в месяц.</li>
        <li>После запуска цена вырастет до {price_pro_open} ₽ в месяц.</li>
        <li>Ответы, подсказки и упражнения в любое время дня.</li>
      </ul>
      <div class="btn-row">
        <button class="btn btn-secondary" onclick="onThink('pro')">Еще подумаю</button>
        <button class="btn btn-primary" onclick="onPay('pro')">Оплатить</button>
      </div>
    </div>

    <div id="view-personal" class="view">
      <div class="nav-back" onclick="showView('intro')"><span class="nav-back-arrow">←</span> Назад</div>
      <h2 class="section-title">Личная работа 1‑на‑1</h2>
      <p class="section-sub">
        Индивидуальное сопровождение, где мы точечно работаем с вашим запросом и жизнью в целом.
      </p>

      <div class="cards" style="margin-top:8px;">
        <div class="card personal-card" data-personal="1m" onclick="selectPersonal('1m')">
          <div class="card-header">
            <div class="card-title">1 месяц плотной личной работы</div>
          </div>
          <div class="price-row">
            <span class="price-main">{price_p1} ₽</span>
          </div>
          <ul class="bullet-list">
            <li>4 плотные личные сессии по 3–4 часа, 1 раз в неделю.</li>
            <li>4 коротких созвона по ходу месяца.</li>
            <li>Сопровождение между встречами и точечная настройка состояния.</li>
          </ul>
        </div>

        <div class="card personal-card" data-personal="2m" onclick="selectPersonal('2m')">
          <div class="card-header">
            <div class="card-title">2 месяца глубокой перестройки</div>
          </div>
          <div class="price-row">
            <span class="price-main">{price_p2} ₽</span>
          </div>
          <ul class="bullet-list">
            <li>8 глубоких сессий по 3–4 часа, 1 раз в неделю.</li>
            <li>Еженедельные сверки по динамике и метрикам.</li>
            <li>Глубокая проработка сценариев и время на закрепление изменений.</li>
          </ul>
        </div>

        <div class="card personal-card" data-personal="4m" onclick="selectPersonal('4m')">
          <div class="card-header">
            <div class="card-title">4 месяца VIP‑сопровождения</div>
          </div>
          <div class="price-row">
            <span class="price-main">{price_p4} ₽</span>
          </div>
          <ul class="bullet-list">
            <li>16 глубоких личных сессий по 3–4 часа.</li>
            <li>Длительное сопровождение и регулярная корректировка состояния.</li>
            <li>Работа не рывком, а через устойчивую интеграцию в жизнь.</li>
          </ul>
        </div>
      </div>

      <p class="card-desc" style="margin-top:8px;">
        После выбора формата я дам отдельные инструкции по оплате и организации работы.
      </p>
      <div class="btn-row">
        <button class="btn btn-secondary" onclick="onThink('personal')">Еще подумаю</button>
        <button class="btn btn-primary" onclick="onPayPersonal()">Оплатить</button>
      </div>
    </div>
  </div>

  <script>
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) {{
      tg.expand();
    }}

    let groupTariff = 'standard';
    let personalChoice = '1m';

    function showView(id) {{
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      const el = document.getElementById('view-' + id);
      if (el) el.classList.add('active');
    }}

    function openProduct(code) {{
      if (code === 'group') {{
        showView('group');
      }} else if (code === 'webinar') {{
        showView('webinar');
      }} else if (code === 'pro') {{
        showView('pro');
      }} else if (code === 'personal') {{
        showView('personal');
      }}
    }}

    function selectGroupTariff(t) {{
      groupTariff = t;
      const std = document.getElementById('pill-group-standard');
      const vip = document.getElementById('pill-group-vip');
      if (std && vip) {{
        std.classList.toggle('active', t === 'standard');
        vip.classList.toggle('active', t === 'vip');
      }}
    }}

    function selectPersonal(code) {{
      personalChoice = code;
      document.querySelectorAll('.personal-card').forEach(function(el) {{
        el.classList.toggle('selected', el.getAttribute('data-personal') === code);
      }});
    }}

    function getQueryParam(name) {{
      const params = new URLSearchParams(window.location.search || '');
      return params.get(name);
    }}

    async function createOrder(productCode) {{
      try {{
        const userId = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) || getQueryParam('user_id');
        const chatId = getQueryParam('chat_id');
        const res = await fetch('/miniapp/create_order', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ product_code: productCode, user_id: userId, chat_id: chatId }}),
        }});
        const data = await res.json();
        if (data && data.url) {{
          window.location.href = data.url;
        }} else {{
          alert('Сейчас оплата недоступна. Попробуй позже.');
        }}
      }} catch (e) {{
        console.error(e);
        alert('Сейчас оплата недоступна. Попробуй позже.');
      }}
    }}

    function onPayGroup() {{
      const code = groupTariff === 'vip' ? 'group_vip' : 'group_standard';
      createOrder(code);
    }}

    function onPay(code) {{
      createOrder(code);
    }}

    function onPayPersonal() {{
      let code = 'personal_1m';
      if (personalChoice === '2m') code = 'personal_2m';
      if (personalChoice === '4m') code = 'personal_4m';
      createOrder(code);
    }}

    function onThink(from) {{
      if (from === 'webinar') {{
        if (tg) {{
          tg.close();
        }} else {{
          alert('Можешь вернуться к чату — я буду на связи.');
        }}
      }} else {{
        openProduct('webinar');
      }}
    }}
  </script>
</body>
</html>"""


@app.api_route("/robokassa/result", methods=["GET", "POST"])
async def robokassa_result(request: Request) -> PlainTextResponse:
    """
    ResultURL: подтверждение оплаты от Robokassa.
    Должен вернуть "OK{InvId}" при успешной проверке подписи.
    """
    params = await _collect_params(request)
    try:
        cfg = RobokassaConfig.from_env()
        db = PaymentsDB.from_env()
        success, inv_id = process_result_url(params, cfg=cfg, db=db)
        body = f"OK{inv_id}" if success else "ERROR"
        return PlainTextResponse(body)
    except Exception as e:
        logger.exception("Robokassa (VM): ResultURL error: %s", e)
        return PlainTextResponse("ERROR")


@app.get("/robokassa/success")
async def robokassa_success(request: Request) -> HTMLResponse:
    """
    SuccessURL: редирект пользователя после оплаты.
    Это НЕ подтверждение оплаты — оно приходит на ResultURL.
    """
    try:
        cfg = RobokassaConfig.from_env()
        params = await _collect_params(request)
        _ = verify_success_url(params, cfg=cfg)
        return HTMLResponse(_success_html())
    except Exception as e:
        logger.exception("Robokassa (VM): SuccessURL error: %s", e)
        return HTMLResponse(
            """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:2rem">
  <p>Не удалось проверить оплату. Если деньги списались — напишите в поддержку.</p>
</body></html>"""
        )


@app.get("/robokassa/fail")
async def robokassa_fail(request: Request) -> HTMLResponse:  # noqa: ARG001
    return HTMLResponse(_fail_html())


@app.get("/miniapp")
async def miniapp_entry(request: Request) -> HTMLResponse:  # noqa: ARG001
    """Точка входа мини-приложения Telegram WebApp."""
    return HTMLResponse(_miniapp_html())


@app.post("/miniapp/create_order")
async def miniapp_create_order(request: Request) -> HTMLResponse:
    """
    Создание заказа из мини-приложения и генерация ссылки оплаты Robokassa.
    Ожидает JSON: { product_code, user_id, chat_id }.
    """
    try:
        payload = await request.json()
    except Exception:
        return HTMLResponse(
            json.dumps({"error": "invalid_json"}),
            media_type="application/json",
            status_code=400,
        )

    product_code = str(payload.get("product_code") or "").strip()
    user_id = int(payload.get("user_id") or 0)
    chat_id = int(payload.get("chat_id") or 0)

    if product_code not in PRODUCTS:
        return HTMLResponse(
            json.dumps({"error": "unknown_product"}),
            media_type="application/json",
            status_code=400,
        )

    try:
        cfg = RobokassaConfig.from_env()
        db = PaymentsDB.from_env()
    except Exception as e:
        logger.exception("Miniapp: config/db error: %s", e)
        return HTMLResponse(
            json.dumps({"error": "config_error"}),
            media_type="application/json",
            status_code=500,
        )

    product = PRODUCTS[product_code]
    amount = str(product["amount"])
    description = str(product["description"])

    inv_id, token = db.create_order(
        user_id=int(user_id),
        chat_id=int(chat_id),
        product_code=str(product_code),
        amount=amount,
        description=description,
    )

    shp = {
        "Shp_user_id": str(user_id),
        "Shp_chat_id": str(chat_id),
        "Shp_product": str(product_code),
        "Shp_order_token": token,
    }

    pay_url = build_payment_url(
        cfg=cfg,
        inv_id=inv_id,
        out_sum=amount,
        description=description,
        shp=shp,
    )

    body = json.dumps({"url": pay_url})
    return HTMLResponse(body, media_type="application/json")

