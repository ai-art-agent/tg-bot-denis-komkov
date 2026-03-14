# -*- coding: utf-8 -*-
"""
Telegram-бот «ИИ-психолог» с ответами через DeepSeek API.
Поддержка: текст, голосовые (Whisper), потоковый вывод.
Перед запуском: заполните .env (TELEGRAM_BOT_TOKEN, DEEPSEEK_API_KEY; для голоса — OPENAI_API_KEY).
Подробно: INSTRUCTIONS.md.
"""

import os
import re
import html
import json
import logging
import logging.handlers
import tempfile
import time
import asyncio
from collections import defaultdict
from typing import Optional, Callable

from robokassa_integration import PaymentsDB, _to_amount_str

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI
from openai import APIStatusError

# ============== НАСТРОЙКИ (уточните под свои ответы из INSTRUCTIONS.md) ==============

# Имя и описание бота (Этап 3)
BOT_NAME = "Психолог Денис Комков"
BOT_DESCRIPTION = (
    "Я психолог, психотерапевт, регрессолог и гипнотерапевт с 10‑летней практикой. "
    "Я здесь, чтобы внимательно выслушать вас, помочь разобраться в вашем состоянии и предложить профессиональные шаги для проработки."
)

# Путь к файлу с системным промптом (рядом с bot.py).
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")
_VALIDATOR_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validator_prompt.txt")
_SIMULATOR_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_simulator_prompt.txt")

load_dotenv()
# Максимальный размер ответа в символах (для промптов). В system_prompt.txt и validator_prompt.txt
# используйте плейсхолдер {{MAX_RESPONSE_CHARS}} — он подставится при загрузке. Можно задать в .env.
try:
    MAX_RESPONSE_CHARS = int(os.getenv("MAX_RESPONSE_CHARS", "350"))
except (TypeError, ValueError):
    MAX_RESPONSE_CHARS = 350
PLACEHOLDER_MAX_RESPONSE = "{{MAX_RESPONSE_CHARS}}"


def _format_price_display(value: str) -> str:
    """Форматирует сумму для отображения в промпте: 24990 -> «24 990»."""
    s = str(value).strip().replace(",", ".").replace(" ", "")
    try:
        n = int(float(s))
        return f"{n:,}".replace(",", " ")
    except (ValueError, TypeError):
        return value


def _load_system_prompt() -> str:
    """Загружает системный промпт из файла system_prompt.txt. Подставляет {{MAX_RESPONSE_CHARS}} и цены из .env."""
    try:
        with open(_PROMPT_PATH, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            raise ValueError("Файл system_prompt.txt пуст.")
        content = content.replace(PLACEHOLDER_MAX_RESPONSE, str(MAX_RESPONSE_CHARS))
        # Цены из .env (должны совпадать с robokassa_server.py и мини‑приложением)
        price_std = os.getenv("PRICE_GROUP_STANDARD_RUB") or os.getenv("PRICE_GROUP_RUB") or "24990"
        price_vip = os.getenv("PRICE_GROUP_VIP_RUB") or os.getenv("PRICE_GROUP_RUB") or "45990"
        price_webinar = os.getenv("PRICE_WEBINAR_RUB") or "2990"
        price_pro = os.getenv("PRICE_PRO_RUB") or "990"
        price_pro_open = os.getenv("PRICE_PRO_OPEN_RUB") or "1990"
        price_p1 = os.getenv("PRICE_PERSONAL_1M_RUB") or "120000"
        price_p2 = os.getenv("PRICE_PERSONAL_2M_RUB") or "180000"
        price_p4 = os.getenv("PRICE_PERSONAL_4M_RUB") or "300000"
        content = content.replace("{{PRICE_GROUP_STANDARD}}", _format_price_display(price_std))
        content = content.replace("{{PRICE_GROUP_VIP}}", _format_price_display(price_vip))
        content = content.replace("{{PRICE_WEBINAR}}", _format_price_display(price_webinar))
        content = content.replace("{{PRICE_PRO}}", _format_price_display(price_pro))
        content = content.replace("{{PRICE_PRO_OPEN}}", _format_price_display(price_pro_open))
        content = content.replace("{{PRICE_PERSONAL_1M}}", _format_price_display(price_p1))
        content = content.replace("{{PRICE_PERSONAL_2M}}", _format_price_display(price_p2))
        content = content.replace("{{PRICE_PERSONAL_4M}}", _format_price_display(price_p4))
        return content
    except FileNotFoundError:
        raise ValueError(
            f"Не найден файл с промптом: {_PROMPT_PATH}. "
            "Положите system_prompt.txt в папку с bot.py."
        )
    except OSError as e:
        raise ValueError(f"Не удалось прочитать system_prompt.txt: {e}") from e


SYSTEM_PROMPT = _load_system_prompt()


def _load_validator_prompt() -> str:
    """Загружает промпт валидатора из validator_prompt.txt. Подставляет {{MAX_RESPONSE_CHARS}}."""
    try:
        with open(_VALIDATOR_PROMPT_PATH, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return ""
        content = content.replace(PLACEHOLDER_MAX_RESPONSE, str(MAX_RESPONSE_CHARS))
        return content
    except FileNotFoundError:
        logging.warning("Файл validator_prompt.txt не найден, валидация отключена.")
        return ""
    except OSError as e:
        logging.warning("Не удалось прочитать validator_prompt.txt: %s", e)
        return ""


VALIDATOR_PROMPT = _load_validator_prompt()
# Валидатор отключён: ответы показываются без проверки и перегенерации. Файл validator_prompt.txt остаётся в проекте.
VALIDATOR_ENABLED = False
# 0 = одна проверка, без перегенерации. 1 = одна перегенерация при отклонении (не используется при VALIDATOR_ENABLED=False).
MAX_VALIDATION_RETRIES = 1


def _load_simulator_prompt() -> str:
    """Загружает промпт симулятора пользователя из user_simulator_prompt.txt (для автодиалога «два бота»)."""
    try:
        with open(_SIMULATOR_PROMPT_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.warning("Файл user_simulator_prompt.txt не найден, автодиалог недоступен.")
        return ""
    except OSError as e:
        logging.warning("Не удалось прочитать user_simulator_prompt.txt: %s", e)
        return ""


SIMULATOR_PROMPT = _load_simulator_prompt()
SIMULATOR_ENABLED = bool(SIMULATOR_PROMPT)

# История диалога: сколько последних пар сообщений хранить (Этап 4). 0 = не хранить.
MAX_HISTORY_MESSAGES = 10

# Максимальная длина ответа ИИ в символах (Этап 2). 0 = без жёсткого лимита.
MAX_RESPONSE_LENGTH = 0

# Текст согласия при /start (Этап 4). Пустая строка = не показывать. Без упоминания бота/ИИ — в соответствии с промптом.
START_DISCLAIMER = (
    "Каждый запрос и каждая ситуация уникальны и требуют индивидуального подхода. "
    "Именно поэтому я здесь, чтобы помочь вам увидеть своё состояние яснее и наметить безопасные шаги дальше."
)

# Контакты поддержки для /support (Этап 4). Оставьте пустым, если команда не нужна.
SUPPORT_TEXT = """При кризисе или тяжёлом состоянии важно обратиться к человеку:
— Телефон доверия: 8-800-2000-122 (бесплатно, Россия)
— Психологическая помощь: ищите службы в своём городе."""

# Политика конфиденциальности для /privacy (Этап 6). Кратко.
PRIVACY_TEXT = "Сообщения обрабатываются для ответа ИИ и не передаются третьим лицам. Мы не храним переписку для аналитики."

# Разрешённые user_id (Этап 6). Пустой список = доступ у всех. Иначе только эти id.
ALLOWED_USER_IDS = []  # Пример: [123456789, 987654321]

# Логирование в файл (Этап 5). True = писать в bot.log и payments.log (см. LOGGING.md).
LOG_TO_FILE = False

# Отладочный лог анкеты: при DEBUG_ANKET_LOG=1 в .env — запись в debug_anket.log при сохранении/отправке анкеты.
DEBUG_ANKET_LOG = (os.getenv("DEBUG_ANKET_LOG") or "").strip().lower() in ("1", "true", "yes")


def _debug_anket_log(message: str, data: dict) -> None:
    """Пишет одну строку в debug_anket.log только если DEBUG_ANKET_LOG включён."""
    if not DEBUG_ANKET_LOG:
        return
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_anket.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "message": message, "data": data}, ensure_ascii=False) + "\n")
    except Exception:
        pass

# Модель DeepSeek (Этап 2): "deepseek-chat" или "deepseek-reasoner"
DEEPSEEK_MODEL = "deepseek-chat"

# Температура и макс. токенов ответа модели (из .env, иначе по умолчанию)
def _float_env(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v.strip()) if v and v.strip() else default
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(v.strip()) if v and v.strip() else default
    except (TypeError, ValueError):
        return default


DEEPSEEK_TEMPERATURE = _float_env("DEEPSEEK_TEMPERATURE", 1.75)
DEEPSEEK_MAX_TOKENS = _int_env("DEEPSEEK_MAX_TOKENS", 4800)

# Потоковый вывод ответа (Этап 2). True = ответ печатается по частям.
STREAM_RESPONSE = True

# Голосовые сообщения: транскрипция через OpenAI Whisper. Нужен OPENAI_API_KEY в .env.
VOICE_ENABLED = True

MINIAPP_URL_BASE = (os.getenv("MINIAPP_URL") or "").strip()
# Файл с текущим URL мини-приложения (заполняется скриптом cloudflared quick tunnel). Приоритет над .env.
MINIAPP_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp_url.txt")

# Кнопки по шагам диалога: ключ = step_id из тега [STEP:step_id] в ответе модели.
STEP_KEYBOARDS = {
    "start_diagnosis": [
        [("Начать диагностику", "Начать диагностику")],
    ],
    "form_address": [
        [("Женщина", "Женская форма обращения"), ("Мужчина", "Мужская форма обращения"), ("Нейтральная", "Нейтральная форма обращения")],
    ],
    "messenger": [
        [("Telegram", "Telegram"), ("Сотовый", "Сотовый"), ("Другое", "Другое")],
    ],
    "conflict": [
        [("1", "1")],
        [("2", "2")],
        [("3", "3")],
        [("Свой вариант", "Свой вариант")],
    ],
    "insight_next": [
        [("Обсудить возможные пути", "Обсудить возможные пути")],
    ],
    "readiness": None,  # строится в _keyboard_for_step (Хочу продолжить / Еще подумаю)
}

READINESS_CONSENT_TEXTS = {
    "да", "да.", "да!", "да да", "да, да",
    "хочу продолжить",
    "да, хочу меняться", "да хочу меняться",
    "да, хочу что-то менять", "да хочу что-то менять",
    "готов", "готов.", "готова", "готова.",
    "согласен", "согласен.", "согласна", "согласна.",
    "соглашусь", "согласна попробовать", "согласен попробовать",
    "конечно", "конечно.", "да конечно", "да, конечно",
    "ок", "ок.", "окей", "ok", "okay",
    "давай", "давай.", "давайте", "давайте.",
    "поехали", "поехали.", "погнали",
    "готов попробовать", "готова попробовать",
    "хочу попробовать", "хочу попробовать.",
    "непременно", "непременно.",
    "с удовольствием", "с удовольствием.",
    "да, готов", "да готов", "да, готова", "да готова",
    "хочу", "хочу.", "буду", "буду.",
    "да, согласен", "да согласен", "да, согласна", "да согласна",
    "подходит", "подходит.", "подойдёт", "подойдёт.",
    "да, подходит", "да подходит",
    "готов действовать", "готова действовать",
    "хочу измениться", "хочу меняться",
}

# Фразы в ответе модели, при которых принудительно показываем кнопку [STEP:readiness], если модель не поставила тег.
READINESS_REPLY_KEYWORDS = (
    "готовы предпринять", "готовность действовать",
    "какой формат", "формат вам откликается", "откликается больше",
    "конкретные шаги", "насколько вы сейчас готовы", "насколько вы готовы",
    "предпринять шаги", "изменить текущую ситуацию",
)

# Если в ответе есть вопрос про инсайт («верно ли уловил суть»), НЕ подставлять кнопки готовности — там нужны «Верно»/«Не совсем».
INSIGHT_FEEDBACK_PHRASES = (
    "уловил суть", "уловила суть", "верно ли я уловил", "верно ли я уловила",
    "верно ли я уловил суть", "верно ли я уловила суть",
    "правильно ли я понял", "правильно ли я поняла",
    "откликается ли вам", "откликается ли тебе",
)

# Кнопки продуктов (callback_data) -> внутренний код продукта для платежей
PRODUCT_BUTTON_TO_CODE = {
    "Групповые занятия": "group",
    "Онлайн вебинар": "webinar",
    "AI-Психолог Pro": "pro",
}

def _amount_from_env(name: str, default: str) -> str:
    v = os.getenv(name, default)
    try:
        return _to_amount_str(v)
    except Exception:
        return _to_amount_str(default)


# Цены только из .env. Для групповых: если нет PRICE_GROUP_STANDARD_RUB/PRICE_GROUP_VIP_RUB, берётся PRICE_GROUP_RUB (обратная совместимость со старым .env на ВМ).
PRICE_GROUP_STANDARD_RUB = _amount_from_env("PRICE_GROUP_STANDARD_RUB", os.getenv("PRICE_GROUP_RUB", "24990"))
PRICE_GROUP_VIP_RUB = _amount_from_env("PRICE_GROUP_VIP_RUB", os.getenv("PRICE_GROUP_RUB", "45990"))
PRICE_WEBINAR_RUB = _amount_from_env("PRICE_WEBINAR_RUB", "2990")
PRICE_PRO_RUB = _amount_from_env("PRICE_PRO_RUB", "990")

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
        "description": "Оплата: AI-Психолог Pro (месяц)",
    },
}

# Формат анкеты (outcome) — совпадает с system_prompt.txt. При сохранении анкет/БД клиентов
# использовать те же ключи: readiness, product, tariff, preferred_contact_time, preferred_group_start.

# Парсинг тега [STEP:step_id] в ответе модели. Ищем последнее вхождение.
STEP_TAG_REGEX = re.compile(r"\[STEP:\s*([\w:]+)\]", re.IGNORECASE)
# Удаляем любой [STEP:xxx] из текста перед показом пользователю (тег служебный).
STEP_TAG_ANYWHERE = re.compile(r"\s*\[STEP:\s*[\w:]+\]\s*", re.IGNORECASE)
# Автогенерация кнопок: [BUTTONS: Текст1 | Текст2 | Текст3] (до 4 кнопок, до 64 байт на callback_data).
BUTTONS_TAG_REGEX = re.compile(r"\s*\[BUTTONS:\s*([^\]]+)\]", re.IGNORECASE)
CALLBACK_DATA_MAX_BYTES = 64

# Маркер списка: только длинное тире «—». Модель может вывести «*» или «-» в начале строки — заменяем на «—».
LIST_MARKER = "—"

# ============== КОД БОТА ==============

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("В .env не указан TELEGRAM_BOT_TOKEN. См. INSTRUCTIONS.md, Этап 1.")
if not DEEPSEEK_API_KEY:
    raise ValueError("В .env не указан DEEPSEEK_API_KEY. См. INSTRUCTIONS.md, Этап 2.")

# DeepSeek API (совместим с OpenAI SDK) — асинхронный клиент для ответов психолога и потокового вывода
client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)
# OpenAI — только для Whisper (голосовые). Если ключа нет, голос отключён.
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# История диалога: один экземпляр БД для доступа к user_history (таблица user_history в PAYMENTS_DB_PATH).
_payments_db: Optional[PaymentsDB] = None


def _get_payments_db() -> PaymentsDB:
    """Ленивая инициализация БД для истории (и оплат при необходимости)."""
    global _payments_db
    if _payments_db is None:
        _payments_db = PaymentsDB.from_env()
    return _payments_db


def _format_reply_for_telegram(text: str) -> tuple[str, Optional[str]]:
    """
    Приводит ответ модели к виду для Telegram:
    - «**текст**» → жирный через HTML <b>, остальное экранируется для HTML.
    - Строки списков «* пункт» / «- пункт» → «— пункт» (длинное тире как маркер буллитов).
    Возвращает (итоговый текст, parse_mode или None). parse_mode="HTML" при наличии тегов.
    """
    if not text:
        return text, None
    # Списки: в начале строки * или - с пробелом → длинное тире «—»
    text = re.sub(r"^(\s*)(\*|-)\s+", rf"\1{LIST_MARKER} ", text, flags=re.MULTILINE)
    # Жирный: **...** → <b>...</b> с экранированием для HTML
    parts = re.split(r"\*\*(.+?)\*\*", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            result.append(html.escape(part))
        else:
            result.append("<b>" + html.escape(part) + "</b>")
    out = "".join(result)
    use_html = "<b>" in out
    return (out, "HTML" if use_html else None)


def _get_reply_target(update: Update):
    """Сообщение, в ответ на которое шлём ответ (при тексте/голосе — message, при нажатии кнопки — callback.message)."""
    if update.message:
        return update.message
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message
    return None


def _strip_step_tags_for_display(text: str) -> str:
    """Удаляет все [STEP:xxx] из текста, чтобы служебный тег не показывался пользователю."""
    if not text or not text.strip():
        return text
    out = STEP_TAG_ANYWHERE.sub(" ", text)
    return re.sub(r"\s+", " ", out).strip() or "…"


def _force_readiness_step_if_relevant(reply_clean: str) -> Optional[str]:
    """Если в ответе модели есть фразы про готовность к шагам/формат — возвращаем 'readiness'. Не подставляем readiness, если это вопрос про инсайт («верно ли уловил суть») — там нужны кнопки «Верно»/«Не совсем»."""
    if not reply_clean or not reply_clean.strip():
        return None
    lower = reply_clean.lower()
    for phrase in INSIGHT_FEEDBACK_PHRASES:
        if phrase in lower:
            return None
    for kw in READINESS_REPLY_KEYWORDS:
        if kw in lower:
            return "readiness"
    return None


def _parse_step_from_reply(reply: str) -> tuple[str, Optional[str]]:
    """Ищет последнее вхождение [STEP:step_id] в ответе, убирает его и всё после него; возвращает (очищенный текст, step_id или None)."""
    matches = list(STEP_TAG_REGEX.finditer(reply))
    if not matches:
        return reply, None
    last = matches[-1]
    step_id = last.group(1).lower()
    # Показываем пользователю только текст до тега (тег и всё после — скрыты).
    reply_clean = reply[: last.start()].rstrip()
    # Для [STEP:custom] после тега идёт [BUTTONS: ...] — оставляем хвост для _parse_custom_buttons.
    if step_id == "custom":
        reply_clean = (reply_clean + " " + reply[last.end() :].lstrip()).strip()
    # Убираем любой оставшийся [STEP:xxx] из текста (модель могла вставить тег в начало или середину).
    reply_clean = STEP_TAG_ANYWHERE.sub(" ", reply_clean)
    reply_clean = re.sub(r"\s+", " ", reply_clean).strip()
    return reply_clean, step_id


def _readiness_label_and_callback(form_address: Optional[str]) -> tuple[str, str]:
    """Подпись и callback кнопки для шага readiness (обезличенно)."""
    return "Хочу продолжить", "Хочу продолжить"


def _get_miniapp_base() -> str:
    """Базовый URL мини-приложения: сначала из файла miniapp_url.txt (от cloudflared), иначе из .env."""
    try:
        if os.path.isfile(MINIAPP_URL_FILE):
            with open(MINIAPP_URL_FILE, "r", encoding="utf-8") as f:
                line = (f.read() or "").strip().split("\n")[0].strip()
                if line:
                    return line
    except Exception:
        pass
    return MINIAPP_URL_BASE or ""


def _build_miniapp_url(chat_id: int, user_id: int) -> Optional[str]:
    base = _get_miniapp_base()
    if not base:
        return None
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}chat_id={chat_id}&user_id={user_id}"


async def _send_miniapp_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет пользователю ссылку на мини-приложение с выбором формата и оплатой. При отсутствии URL — «Что-то пошло не так» + кнопка «Попробовать ещё раз»."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    url = _build_miniapp_url(chat.id, user.id)
    if not url:
        kb_retry = InlineKeyboardMarkup([
            [InlineKeyboardButton("Попробовать ещё раз", callback_data="miniapp_retry")],
        ])
        target = _get_reply_target(update)
        if target:
            await target.reply_text(
                "Что-то пошло не так. Попробуйте ещё раз.",
                reply_markup=kb_retry,
            )
        return

    # Telegram открывает Web App (встроенное окно) только по HTTPS. Для HTTP даём обычную ссылку — откроется в браузере.
    if url.startswith("https://"):
        btn = InlineKeyboardButton("Выбрать формат и оплатить", web_app=WebAppInfo(url=url))
    else:
        btn = InlineKeyboardButton("Выбрать формат и оплатить", url=url)
    kb = InlineKeyboardMarkup([[btn]])

    target = _get_reply_target(update)
    if target:
        await target.reply_text(
            "Ты сделал важный шаг — признал, что готов что-то менять. "
            "Нажми кнопку ниже: откроется окно с выбором формата работы и оплатой."
            + (" (По ссылке http откроется в браузере — для окна внутри Telegram нужен HTTPS.)" if url.startswith("http://") else ""),
            reply_markup=kb,
            disable_web_page_preview=True,
        )


def _keyboard_for_step(
    step_id: str,
    context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    chat_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура по step_id; для readiness при заданных chat_id/user_id и MINIAPP_URL — кнопка «Хочу продолжить» открывает мини-приложение. Telegram принимает только HTTPS для Web App; при HTTP показываем callback, по нажатию отправим сообщение со ссылкой."""
    if step_id == "readiness":
        miniapp_url = _build_miniapp_url(chat_id, user_id) if (chat_id is not None and user_id is not None) else None
        if miniapp_url and miniapp_url.startswith("https://"):
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("Хочу продолжить", web_app=WebAppInfo(url=miniapp_url))],
                [InlineKeyboardButton("Еще подумаю", callback_data="Еще подумаю")],
            ])
        label, callback = _readiness_label_and_callback(
            context.user_data.get("form_address") if context else None
        )
        rows = [[(label, callback), ("Еще подумаю", "Еще подумаю")]]
        return InlineKeyboardMarkup([[InlineKeyboardButton(str(btn_label), callback_data=str(btn_cb)) for btn_label, btn_cb in row] for row in rows])

    rows = STEP_KEYBOARDS.get(step_id)
    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(str(label), callback_data=str(cb)) for label, cb in row] for row in rows]
    return InlineKeyboardMarkup(keyboard)


def _truncate_callback_data(s: str, max_bytes: int = CALLBACK_DATA_MAX_BYTES) -> str:
    """Обрезает строку до max_bytes в UTF-8 (лимит Telegram для callback_data)."""
    data = s.strip().encode("utf-8")
    if len(data) <= max_bytes:
        return s.strip()
    return data[:max_bytes].decode("utf-8", errors="ignore").strip() or s[:1]


def _parse_custom_buttons(reply: str) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Ищет в ответе тег [BUTTONS: Текст1 | Текст2 | ...], строит клавиатуру (до 4 кнопок),
    удаляет тег из текста. Возвращает (очищенный текст, клавиатура или None).
    """
    m = BUTTONS_TAG_REGEX.search(reply)
    if not m:
        return reply, None
    raw = m.group(1).strip()
    labels = [part.strip() for part in re.split(r"\s*\|\s*", raw) if part.strip()][:4]
    if not labels:
        return reply[: m.start()].rstrip() + reply[m.end() :].lstrip(), None
    rows = [[(label, _truncate_callback_data(label))] for label in labels]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(str(label), callback_data=cb) for label, cb in row] for row in rows])
    cleaned = (reply[: m.start()].rstrip() + " " + reply[m.end() :].lstrip()).strip()
    return cleaned, keyboard


def get_history_messages(user_id: int) -> list[dict]:
    """Возвращает список сообщений для API OpenAI в формате role/content."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in _get_payments_db().get_user_history(user_id):
        messages.append({"role": item["role"], "content": item["content"]})
    return messages


def add_to_history(user_id: int, role: str, content: str) -> None:
    _get_payments_db().append_user_message(user_id, role, content, max_pairs=MAX_HISTORY_MESSAGES or 0)


def clear_history(user_id: int) -> None:
    _get_payments_db().clear_user_history(user_id)


def _remove_last_from_history(user_id: int) -> None:
    """Удаляет последнее сообщение из истории (при ошибке API)."""
    db = _get_payments_db()
    messages = db.get_user_history(user_id)
    if messages:
        messages.pop()
        db.set_user_history(user_id, messages)


def truncate_response(text: str) -> str:
    if MAX_RESPONSE_LENGTH <= 0:
        return text
    if len(text) <= MAX_RESPONSE_LENGTH:
        return text
    return text[: MAX_RESPONSE_LENGTH - 3].rstrip() + "..."


async def check_access(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in ALLOWED_USER_IDS:
        if update.message:
            await update.message.reply_text("Доступ к боту ограничен.")
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Доступ ограничен.")
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    text = "Привет. Нажми кнопку ниже, чтобы начать разговор."
    if START_DISCLAIMER:
        text += "\n\n" + START_DISCLAIMER
    keyboard = [[InlineKeyboardButton("Начать", callback_data="start_chat")]]
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.message.reply_text(
        "Команды: /start — начало разговора, /help — эта справка."
        + (" /support — контакты поддержки." if SUPPORT_TEXT else "")
        + (" /privacy — конфиденциальность." if PRIVACY_TEXT else "")
        + (" /new — начать диалог заново (сбросить контекст)." if MAX_HISTORY_MESSAGES else "")
    )


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if not SUPPORT_TEXT:
        await update.message.reply_text("Команда не настроена.")
        return
    await update.message.reply_text(SUPPORT_TEXT)


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if not PRIVACY_TEXT:
        await update.message.reply_text("Команда не настроена.")
        return
    await update.message.reply_text(PRIVACY_TEXT)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сброс контекста диалога (команда /new)."""
    if not await check_access(update):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    clear_history(user_id)
    await update.message.reply_text("Контекст сброшен. Можешь начать разговор заново — напиши сообщение или нажми /start.")

async def button_new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await query.edit_message_text("Доступ ограничен.")
        return
    had_history = len(_get_payments_db().get_user_history(user_id)) > 0
    clear_history(user_id)
    if had_history:
        await query.edit_message_text("Контекст сброшен. Можешь начать новый разговор.")
    else:
        await query.edit_message_text("История пуста. Напиши сообщение — и мы начнём.")


async def button_start_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Начать» при /start — запускает первый ответ бота (как если бы пользователь написал «Начать»)."""
    if not update.callback_query:
        return
    await update.callback_query.answer()
    if not await check_access(update):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    await _reply_to_user(update, context, user_id, "Начать")


def _apply_product_and_tariff_from_text(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """
    По тексту пользователя (например «ВИП», «Групповые занятия») выставляет
    context.user_data["selected_product"] и при необходимости ["group_tariff"],
    чтобы кнопка «Оплатить» сработала и при ответе текстом, а не только по кнопке.
    """
    if not text:
        return
    t = text.strip()
    # Точное совпадение с кнопками продуктов
    if t in PRODUCT_BUTTON_TO_CODE:
        context.user_data["selected_product"] = PRODUCT_BUTTON_TO_CODE[t]
        return
    # ВИП / VIP — тариф групповых
    if t.upper() in ("ВИП", "VIP"):
        context.user_data["group_tariff"] = "vip"
        if context.user_data.get("selected_product") is None:
            context.user_data["selected_product"] = "group"
        return
    # Стандарт — тариф групповых
    if t.lower() == "стандарт":
        context.user_data["group_tariff"] = "standard"
        if context.user_data.get("selected_product") is None:
            context.user_data["selected_product"] = "group"


async def handle_step_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатия кнопки шага: callback_data уходит в модель как ответ пользователя."""
    if not update.callback_query:
        return
    if not await check_access(update):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.callback_query.answer()
        return
    await update.callback_query.answer()
    user_text = (update.callback_query.data or "").strip()
    if not user_text:
        return

    # Запоминаем форму обращения для кнопки readiness.
    if user_text in ("Мужская форма обращения", "Женская форма обращения", "Нейтральная форма обращения"):
        context.user_data["form_address"] = user_text

    # Переход в мини-приложение после «Хочу продолжить» или согласия текстом.
    if user_text.strip().lower() in READINESS_CONSENT_TEXTS:
        await _send_miniapp_entry(update, context)
        return

    # «Попробовать ещё раз» — повторно читаем URL из файла и показываем мини-приложение или снова ошибку с кнопкой.
    if user_text == "miniapp_retry":
        await _send_miniapp_entry(update, context)
        return

    # «Еще думаю» — сохраняем анкету из контекста, затем запрашиваем у модели полный JSON и сохраняем в clients.
    if user_text == "Еще думаю":
        _save_anket_after_refusal(update, context)
        try:
            messages = get_history_messages(user_id) + [{"role": "user", "content": "SHOW_JSON"}]
            reply_raw = await _generate_reply(messages, stream=False)
            _save_anket_from_show_json(update, reply_raw)
        except Exception as e:
            logging.exception("Еще думаю: сохранение полной анкеты из модели: %s", e)

    await _reply_to_user(update, context, user_id, user_text)


async def _generate_reply(msgs: list[dict], stream: bool = False, on_chunk: Optional[Callable[[str], None]] = None) -> str:
    """Генерация ответа модели. При stream=True и on_chunk вызывается on_chunk(accumulated) для каждого фрагмента (on_chunk может быть async)."""
    if stream:
        stream_obj = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=msgs,
            max_tokens=DEEPSEEK_MAX_TOKENS,
            temperature=DEEPSEEK_TEMPERATURE,
            stream=True,
        )
        accumulated = ""
        async for chunk in stream_obj:
            if chunk.choices and chunk.choices[0].delta.content:
                accumulated += chunk.choices[0].delta.content
                if on_chunk:
                    try:
                        result = on_chunk(accumulated)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass
        return truncate_response(accumulated.strip()) or "Не удалось сформировать ответ."
    response = await client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=msgs,
        max_tokens=DEEPSEEK_MAX_TOKENS,
        temperature=DEEPSEEK_TEMPERATURE,
        stream=False,
    )
    raw = response.choices[0].message.content or ""
    return truncate_response(raw.strip()) or "Не удалось сформировать ответ."


async def get_bot_reply(
    user_id: int,
    user_text: str,
    context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    log_validator_full: bool = False,
    validator_callback: Optional[Callable[[str], None]] = None,
    stream_callback: Optional[Callable[[str], None]] = None,
):
    """
    Один шаг диалога без Telegram. Возвращает (reply_clean, buttons, validator_outputs, timings, rejected_reply_clean).
    Валидатор отключён: validator_outputs всегда [], rejected_reply_clean всегда None.
    stream_callback(text_so_far): при задании ответ психолога стримится по фрагментам.
    """
    add_to_history(user_id, "user", user_text)
    messages = get_history_messages(user_id)
    use_stream = stream_callback is not None
    t0_psych = time.monotonic()
    reply_raw = await _generate_reply(
        messages, stream=use_stream, on_chunk=stream_callback if use_stream else None
    )
    psychologist_ms = int((time.monotonic() - t0_psych) * 1000)

    reply_clean, step_id = _parse_step_from_reply(reply_raw)
    if step_id is None:
        step_id = _force_readiness_step_if_relevant(reply_clean)
    keyboard = _keyboard_for_step(step_id, context) if step_id else None
    if keyboard is None:
        reply_clean, keyboard = _parse_custom_buttons(reply_clean)
    add_to_history(user_id, "assistant", reply_clean or "")
    buttons = []
    if keyboard and hasattr(keyboard, "inline_keyboard"):
        for row in keyboard.inline_keyboard:
            for btn in row:
                buttons.append((getattr(btn, "text", ""), getattr(btn, "callback_data", "")))
    timings = {"psychologist_ms": psychologist_ms}
    return (reply_clean or "").strip(), buttons, [], timings, None


def _is_terminal_action(simulator_message: str) -> bool:
    """Проверяет, что симулятор выбрал оплату или отказ — диалог можно завершать и запрашивать SHOW_JSON."""
    s = (simulator_message or "").strip()
    return s.startswith("pay:") or s in ("Еще думаю", "Оплатить")


async def get_simulator_reply(user_id: int, buttons: list[tuple]) -> str:
    """
    Один ответ «пользователя» от второго бота (симулятор). Используется в автодиалоге «два бота».
    Возвращает одну строку: либо текст от имени пользователя, либо callback_data кнопки.
    """
    if not SIMULATOR_ENABLED:
        raise RuntimeError("Симулятор отключён: отсутствует user_simulator_prompt.txt")
    messages = get_history_messages(user_id)
    # Без system, только диалог
    parts = []
    for m in messages:
        if m.get("role") == "system":
            continue
        who = "Психолог" if m.get("role") == "assistant" else "Пользователь"
        parts.append(f"{who}: {m.get('content', '')}")
    conv = "\n\n".join(parts)
    if buttons:
        lines = [f"- {label} -> {cb}" for label, cb in buttons]
        conv += "\n\nТекущие кнопки (ответь ровно одним callback_data или своим текстом):\n" + "\n".join(lines)
    else:
        conv += "\n\nКнопок нет. Ответь текстом от имени пользователя."
    response = await client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": SIMULATOR_PROMPT},
            {"role": "user", "content": conv},
        ],
        max_tokens=200,
        temperature=0.7,
    )
    raw = (response.choices[0].message.content or "").strip()
    # Одна строка: берём первую, обрезаем по переносу
    return raw.split("\n")[0].strip() if raw else ""


def _extract_anket_json_from_reply(reply_text: str) -> dict | None:
    """Извлекает JSON анкеты из ответа модели (SHOW_JSON). Возвращает dict или None."""
    if not reply_text or not isinstance(reply_text, str):
        return None
    text = reply_text.strip()
    # Блок ```json ... ``` или ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass
    # Первый объект { ... }
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, TypeError):
                    return None
    return None


def _anket_flat_from_parsed(parsed: dict, user_id: int, chat_id: int, username: str | None, first_name: str | None, last_name: str | None) -> dict:
    """Сопоставляет распарсенный JSON анкеты (system_prompt) с плоскими полями для clients."""
    contact = parsed.get("contact") or {}
    profile = parsed.get("profile") or {}
    diagnostic = parsed.get("diagnostic") or {}
    outcome = parsed.get("outcome") or {}
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "username": (parsed.get("username") or username or "").strip() or None,
        "first_name": (parsed.get("first_name") or first_name or "").strip() or None,
        "last_name": (parsed.get("last_name") or last_name or "").strip() if (parsed.get("last_name") or last_name) else None,
        "contact_channel": (contact.get("channel") or "").strip() or None,
        "contact_value": (contact.get("value") or "").strip() or None,
        "profile_name": (profile.get("name") or "").strip() or None,
        "form_address": (profile.get("form_address") or "").strip() or None,
        "age_group": (profile.get("age_group") or "").strip() or None,
        "focus": (diagnostic.get("focus") or "").strip() or None,
        "duration": (diagnostic.get("duration") or "").strip() or None,
        "previous_attempts": (diagnostic.get("previous_attempts") or "").strip() or None,
        "conflict": (diagnostic.get("conflict") or "").strip() or None,
        "self_value_scale": diagnostic.get("self_value_scale") if isinstance(diagnostic.get("self_value_scale"), (int, float)) else None,
        "insight": (diagnostic.get("insight") or "").strip() or None,
        "readiness": (outcome.get("readiness") or "").strip() or None,
        "product": (outcome.get("product") or "").strip() or None,
        "tariff": (outcome.get("tariff") or "").strip() or None,
        "preferred_contact_time": (outcome.get("preferred_contact_time") or "").strip() or None,
        "preferred_group_start": (outcome.get("preferred_group_start") or "").strip() or None,
        "anket_json": json.dumps(parsed, ensure_ascii=False) if parsed else None,
    }


def _save_anket_from_show_json(update: Update, reply_clean: str, db: Optional["PaymentsDB"] = None) -> None:
    """После ответа на SHOW_JSON парсит JSON из ответа и сохраняет анкету в clients. db опционально — при отсутствии создаётся из env."""
    if (reply_clean or "").strip() == "":
        return
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    parsed = _extract_anket_json_from_reply(reply_clean)
    if not parsed or not isinstance(parsed, dict):
        return
    try:
        if db is None:
            db = _get_payments_db()
        flat = _anket_flat_from_parsed(
            parsed,
            user_id=user.id,
            chat_id=chat.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        to_save = {k: v for k, v in flat.items() if k != "user_id" and v is not None}
        db.upsert_client(**to_save, user_id=flat["user_id"])
        _debug_anket_log("anket_saved", {"user_id": user.id, "fields": len(to_save)})
    except Exception as e:
        logging.exception("Сохранение анкеты (SHOW_JSON): %s", e)


def _save_anket_after_refusal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Сохраняет/обновляет анкету клиента после нажатия «Еще думаю» (отказ от оплаты).
    Один клиент = одна строка по user_id (или username при необходимости).
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    try:
        db = _get_payments_db()
        db.upsert_client(
            user_id=user.id,
            chat_id=chat.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            form_address=context.user_data.get("form_address"),
            product=context.user_data.get("selected_product"),
            tariff=context.user_data.get("group_tariff"),
            readiness="Еще подумаю",
        )
    except Exception as e:
        logging.exception("Сохранение анкеты (Еще думаю): %s", e)


async def _reply_to_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    user_text: str,
) -> None:
    """Общая логика: добавить в историю, вызвать DeepSeek, отправить ответ (без валидатора)."""
    add_to_history(user_id, "user", user_text)
    messages = get_history_messages(user_id)
    target = _get_reply_target(update)
    chat = update.effective_chat
    if not target or not chat:
        return

    await chat.send_action("typing")

    try:
        sent_msg = await target.reply_text("…")

        # Потоковый вывод. Троттлинг ~0.2 с.
        last_stream_edit = [0.0]
        STREAM_THROTTLE_SEC = 0.2

        async def stream_edit(accumulated: str) -> None:
            display, _ = _parse_step_from_reply(accumulated)
            # Убираем из показа любые фрагменты вида [...]
            display = re.sub(r"\[[^\]]*\]", "", display or "")
            display = re.sub(r"  +", " ", display).strip()
            display = display or "…"
            if len(display) > 4090:
                display = display[:4090] + "..."
            now = time.monotonic()
            if now - last_stream_edit[0] >= STREAM_THROTTLE_SEC or not last_stream_edit[0]:
                try:
                    await sent_msg.edit_text(display or "…")
                    last_stream_edit[0] = now
                except Exception:
                    pass

        reply_raw = await _generate_reply(messages, stream=True, on_chunk=stream_edit)

        reply_clean, step_id = _parse_step_from_reply(reply_raw)
        if step_id is None:
            step_id = _force_readiness_step_if_relevant(reply_clean)
        keyboard = _keyboard_for_step(step_id, context, chat_id=chat.id, user_id=user_id) if step_id else None
        if keyboard is None:
            reply_clean, keyboard = _parse_custom_buttons(reply_clean)
        final_text = reply_clean[:4096] if len(reply_clean) > 4096 else reply_clean
        final_text, parse_mode = _format_reply_for_telegram(final_text)
        if len(final_text) > 4096:
            final_text = final_text[:4093] + "..."

        try:
            await sent_msg.edit_text(
                final_text,
                parse_mode=parse_mode if parse_mode else None,
                reply_markup=keyboard,
            )
        except Exception:
            pass
        add_to_history(user_id, "assistant", reply_clean or "")
    except APIStatusError as e:
        _remove_last_from_history(user_id)
        err_text = (
            "Сейчас сервис ответов временно недоступен (исчерпан баланс API). Попробуй позже или обратись к администратору бота."
            if e.status_code == 402
            else "Что-то пошло не так при ответе. Попробуй ещё раз или позже."
        )
        try:
            await sent_msg.edit_text(err_text)
        except Exception:
            await target.reply_text(err_text)
    except Exception as e:
        logging.exception("DeepSeek API error: %s", e)
        _remove_last_from_history(user_id)
        try:
            await sent_msg.edit_text("Что-то пошло не так при ответе. Попробуй ещё раз или позже.")
        except Exception:
            await target.reply_text("Что-то пошло не так при ответе. Попробуй ещё раз или позже.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши текстом, пожалуйста.")
        return

    # Переход в мини-приложение после текстового согласия «готов меняться».
    if text.strip().lower() in READINESS_CONSENT_TEXTS:
        await _send_miniapp_entry(update, context)
        return

    # Служебная команда SHOW_JSON — вызываем модель для получения JSON анкеты, сохраняем в clients, клиенту показываем только «Запрос принят».
    if text == "SHOW_JSON":
        add_to_history(user_id, "user", text)
        try:
            messages = get_history_messages(user_id)
            reply_raw = await _generate_reply(messages, stream=False)
            _save_anket_from_show_json(update, reply_raw)
        except Exception as e:
            logging.exception("SHOW_JSON: генерация/сохранение анкеты: %s", e)
        add_to_history(user_id, "assistant", "Запрос принят. Можем продолжить разговор.")
        await update.message.reply_text("Запрос принят. Можем продолжить разговор.")
        return

    # Сохраняем выбор продукта/тарифа и при текстовом ответе (напр. «ВИП», «Групповые занятия»),
    # чтобы кнопка «Оплатить» потом работала.
    _apply_product_and_tariff_from_text(context, text)

    await _reply_to_user(update, context, user_id, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if not VOICE_ENABLED or not openai_client:
        await update.message.reply_text(
            "Голосовые сообщения пока не настроены. Напиши текстом."
        )
        return

    user_id = update.effective_user.id
    voice = update.message.voice
    await update.message.chat.send_action("typing")

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        try:
            file = await context.bot.get_file(voice.file_id)
            await file.download_to_drive(tmp.name)
        except Exception as e:
            logging.exception("Voice download error: %s", e)
            await update.message.reply_text("Не удалось загрузить голосовое сообщение.")
            return

    try:
        with open(tmp.name, "rb") as audio_file:
            transcript = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        user_text = (transcript.text or "").strip()
    except Exception as e:
        logging.exception("Whisper transcription error: %s", e)
        await update.message.reply_text("Не удалось распознать голос. Попробуй написать текстом.")
        return
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if not user_text:
        await update.message.reply_text("Текст не распознан. Попробуй ещё раз или напиши.")
        return

    # Переход в мини-приложение после голосового согласия «готов меняться».
    if user_text.strip().lower() in READINESS_CONSENT_TEXTS:
        await _send_miniapp_entry(update, context)
        return

    # Служебная команда SHOW_JSON — вызываем модель для получения JSON анкеты, сохраняем в clients, клиенту показываем только «Запрос принят».
    if user_text == "SHOW_JSON":
        add_to_history(user_id, "user", user_text)
        try:
            messages = get_history_messages(user_id)
            reply_raw = await _generate_reply(messages, stream=False)
            _save_anket_from_show_json(update, reply_raw)
        except Exception as e:
            logging.exception("SHOW_JSON (voice): генерация/сохранение анкеты: %s", e)
        add_to_history(user_id, "assistant", "Запрос принят. Можем продолжить разговор.")
        await update.message.reply_text("Запрос принят. Можем продолжить разговор.")
        return

    _apply_product_and_tariff_from_text(context, user_text)

    await update.message.reply_text(f"🎤 Ты сказал(а): {user_text}")
    await _reply_to_user(update, context, user_id, user_text)


def build_application() -> Application:
    """Собирает и возвращает приложение бота (для polling или webhook)."""
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    if MAX_HISTORY_MESSAGES:
        app.add_handler(CommandHandler("new", cmd_new))
    if SUPPORT_TEXT:
        app.add_handler(CommandHandler("support", cmd_support))
    if PRIVACY_TEXT:
        app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CallbackQueryHandler(button_new_dialog, pattern="^new_dialog$"))
    app.add_handler(CallbackQueryHandler(button_start_chat, pattern="^start_chat$"))
    app.add_handler(CallbackQueryHandler(handle_step_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    if VOICE_ENABLED:
        app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    return app


async def process_webhook_update(update_body: str) -> None:
    """
    Обрабатывает один update от Telegram (режим webhook).
    Для использования в Cloud Functions: передайте сюда тело HTTP-запроса (JSON).
    """
    import json
    app = build_application()
    update_data = json.loads(update_body)
    update = Update.de_json(update_data, app.bot)
    await app.initialize()
    try:
        await app.process_update(update)
    finally:
        await app.shutdown()


# Ротация логов: размер одного файла (байт) и число резервных копий (см. LOGGING.md и INSTRUCTIONS.md).
LOG_BOT_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
LOG_BOT_BACKUP_COUNT = 2
LOG_PAYMENTS_MAX_BYTES = 5 * 1024 * 1024
LOG_PAYMENTS_BACKUP_COUNT = 2


def main() -> None:
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    if LOG_TO_FILE:
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.handlers.clear()
        bot_handler = logging.handlers.RotatingFileHandler(
            "bot.log",
            maxBytes=LOG_BOT_MAX_BYTES,
            backupCount=LOG_BOT_BACKUP_COUNT,
            encoding="utf-8",
        )
        bot_handler.setFormatter(logging.Formatter(log_format))
        root.addHandler(bot_handler)
        pay_logger = logging.getLogger("robokassa_integration")
        pay_handler = logging.handlers.RotatingFileHandler(
            "payments.log",
            maxBytes=LOG_PAYMENTS_MAX_BYTES,
            backupCount=LOG_PAYMENTS_BACKUP_COUNT,
            encoding="utf-8",
        )
        pay_handler.setFormatter(logging.Formatter(log_format))
        pay_logger.addHandler(pay_handler)
        pay_logger.setLevel(logging.INFO)
    else:
        logging.basicConfig(format=log_format, level=logging.INFO)

    app = build_application()
    print("Бот запущен. Остановка: Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
