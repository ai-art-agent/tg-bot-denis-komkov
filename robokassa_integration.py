"""
Интеграция Robokassa: платёжные ссылки, проверка подписей ResultURL/SuccessURL.

Соответствует официальной документации:
  https://docs.robokassa.ru/ru/quick-start
  https://docs.robokassa.ru/ru/pay-interface
  https://docs.robokassa.ru/ru/notifications-and-redirects
  https://docs.robokassa.ru/ru/testing-mode — при IsTest=1 обязательны ТЕСТОВЫЕ пароли из «Технические настройки».
"""
from __future__ import annotations

import hashlib
import os
import time
import json
import logging
import sqlite3
import secrets
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _to_amount_str(value: str | int | float | Decimal) -> str:
    if isinstance(value, str):
        s = value.strip().replace(",", ".")
        d = Decimal(s)
    else:
        d = Decimal(str(value))
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(d, "f")


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _extract_shp(params: dict[str, Any]) -> dict[str, str]:
    shp: dict[str, str] = {}
    for k, v in params.items():
        if not isinstance(k, str):
            continue
        if not k.startswith("Shp_"):
            continue
        if v is None:
            continue
        shp[k] = str(v)
    return shp


def _shp_signature_part(shp: dict[str, str]) -> str:
    if not shp:
        return ""
    items = sorted(shp.items(), key=lambda kv: kv[0])
    return ":" + ":".join([f"{k}={v}" for k, v in items])


@dataclass(frozen=True)
class RobokassaConfig:
    merchant_login: str
    password1: str
    password2: str
    merchant_url: str
    is_test: bool

    @staticmethod
    def from_env() -> "RobokassaConfig":
        merchant_login = _env("ROBOKASSA_MERCHANT_LOGIN")
        merchant_url = _env(
            "ROBOKASSA_MERCHANT_URL",
            "https://auth.robokassa.ru/Merchant/Index.aspx",
        )
        is_test = (_env("ROBOKASSA_IS_TEST", "0") or "0") in ("1", "true", "True", "yes", "YES")

        if is_test:
            password1 = _env("ROBOKASSA_PASSWORD1_TEST") or _env("ROBOKASSA_PASSWORD1")
            password2 = _env("ROBOKASSA_PASSWORD2_TEST") or _env("ROBOKASSA_PASSWORD2")
        else:
            password1 = _env("ROBOKASSA_PASSWORD1")
            password2 = _env("ROBOKASSA_PASSWORD2")

        if not merchant_login:
            raise ValueError("Не задана переменная окружения ROBOKASSA_MERCHANT_LOGIN")
        if not password1:
            raise ValueError(
                "Не задана переменная окружения ROBOKASSA_PASSWORD1"
                + (" или ROBOKASSA_PASSWORD1_TEST (при ROBOKASSA_IS_TEST=1)" if is_test else "")
            )
        if not password2:
            raise ValueError(
                "Не задана переменная окружения ROBOKASSA_PASSWORD2"
                + (" или ROBOKASSA_PASSWORD2_TEST (при ROBOKASSA_IS_TEST=1)" if is_test else "")
            )

        return RobokassaConfig(
            merchant_login=merchant_login,
            password1=password1,
            password2=password2,
            merchant_url=merchant_url,
            is_test=is_test,
        )


class PaymentsDB:
    """
    Простой SQLite-реестр заказов.
    Подходит для VPS/VM. Для serverless лучше вынести в внешнюю БД, но это даст
    рабочий "скелет" интеграции без дополнительных сервисов.
    """

    def __init__(self, path: str):
        self.path = path
        self._init()

    @staticmethod
    def from_env() -> "PaymentsDB":
        path = _env("PAYMENTS_DB_PATH", "payments.sqlite3") or "payments.sqlite3"
        return PaymentsDB(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    inv_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_token TEXT NOT NULL,
                    user_id INTEGER,
                    chat_id INTEGER,
                    product_code TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    paid_at INTEGER,
                    raw_result_params TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    contact_channel TEXT,
                    contact_value TEXT,
                    profile_name TEXT,
                    form_address TEXT,
                    age_group TEXT,
                    focus TEXT,
                    duration TEXT,
                    previous_attempts TEXT,
                    conflict TEXT,
                    self_value_scale INTEGER,
                    insight TEXT,
                    readiness TEXT,
                    product TEXT,
                    tariff TEXT,
                    preferred_contact_time TEXT,
                    preferred_group_start TEXT,
                    anket_json TEXT,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    user_id INTEGER PRIMARY KEY,
                    history_json TEXT NOT NULL DEFAULT '[]',
                    updated_at INTEGER NOT NULL
                )
                """
            )
        finally:
            conn.close()

    def create_order(
        self,
        *,
        user_id: int,
        chat_id: int,
        product_code: str,
        amount: str,
        description: str,
    ) -> tuple[int, str]:
        token = secrets.token_urlsafe(16)
        now = int(time.time())
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO orders (order_token, user_id, chat_id, product_code, amount, description, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (token, user_id, chat_id, product_code, amount, description, now),
            )
            inv_id = int(cur.lastrowid)
            return inv_id, token
        finally:
            conn.close()

    def get_order(self, inv_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM orders WHERE inv_id=?", (inv_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()

    def mark_paid_if_pending(self, inv_id: int, *, raw_params: dict[str, Any]) -> bool:
        """
        Идемпотентно помечает заказ оплаченным.
        Возвращает True, если статус изменили с pending -> paid.
        """
        now = int(time.time())
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE orders
                SET status='paid', paid_at=?, raw_result_params=?
                WHERE inv_id=? AND status='pending'
                """,
                (now, json.dumps(raw_params, ensure_ascii=False), inv_id),
            )
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_group_orders_paid_since(self, since_ts: int) -> list[dict[str, Any]]:
        """
        Возвращает заказы по групповым занятиям (group_standard, group_vip) с status='paid'
        и paid_at >= since_ts (unix timestamp UTC). Сортировка по paid_at по возрастанию.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT inv_id, user_id, chat_id, product_code, amount, description, paid_at
                FROM orders
                WHERE product_code IN ('group_standard', 'group_vip')
                  AND status = 'paid'
                  AND paid_at >= ?
                ORDER BY paid_at ASC
                """,
                (since_ts,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def upsert_client(
        self,
        *,
        user_id: int,
        chat_id: int | None = None,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        contact_channel: str | None = None,
        contact_value: str | None = None,
        profile_name: str | None = None,
        form_address: str | None = None,
        age_group: str | None = None,
        focus: str | None = None,
        duration: str | None = None,
        previous_attempts: str | None = None,
        conflict: str | None = None,
        self_value_scale: int | None = None,
        insight: str | None = None,
        readiness: str | None = None,
        product: str | None = None,
        tariff: str | None = None,
        preferred_contact_time: str | None = None,
        preferred_group_start: str | None = None,
        anket_json: str | None = None,
    ) -> None:
        """
        Создаёт или обновляет запись клиента (анкета). По user_id.
        Пустые значения не перезаписывают существующие (при обновлении).
        """
        now = int(time.time())
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT user_id FROM clients WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing:
                updates = []
                params = []
                for key, val in [
                    ("chat_id", chat_id),
                    ("username", username),
                    ("first_name", first_name),
                    ("last_name", last_name),
                    ("contact_channel", contact_channel),
                    ("contact_value", contact_value),
                    ("profile_name", profile_name),
                    ("form_address", form_address),
                    ("age_group", age_group),
                    ("focus", focus),
                    ("duration", duration),
                    ("previous_attempts", previous_attempts),
                    ("conflict", conflict),
                    ("self_value_scale", self_value_scale),
                    ("insight", insight),
                    ("readiness", readiness),
                    ("product", product),
                    ("tariff", tariff),
                    ("preferred_contact_time", preferred_contact_time),
                    ("preferred_group_start", preferred_group_start),
                    ("anket_json", anket_json),
                ]:
                    if val is not None:
                        updates.append(f"{key} = ?")
                        params.append(val)
                if updates:
                    updates.append("updated_at = ?")
                    params.append(now)
                    params.append(user_id)
                    conn.execute(
                        "UPDATE clients SET " + ", ".join(updates) + " WHERE user_id = ?",
                        params,
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO clients (
                        user_id, chat_id, username, first_name, last_name,
                        contact_channel, contact_value, profile_name, form_address, age_group,
                        focus, duration, previous_attempts, conflict, self_value_scale,
                        insight, readiness, product, tariff, preferred_contact_time, preferred_group_start,
                        anket_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        chat_id,
                        username or "",
                        first_name or "",
                        last_name or "",
                        contact_channel or "",
                        contact_value or "",
                        profile_name or "",
                        form_address or "",
                        age_group or "",
                        focus or "",
                        duration or "",
                        previous_attempts or "",
                        conflict or "",
                        self_value_scale,
                        insight or "",
                        readiness or "",
                        product or "",
                        tariff or "",
                        preferred_contact_time or "",
                        preferred_group_start or "",
                        anket_json or "",
                        now,
                    ),
                )
        finally:
            conn.close()

    def upsert_client_from_order(self, order: dict[str, Any]) -> None:
        """
        Создаёт или обновляет запись клиента по данным заказа (после оплаты).
        Идентификация только по user_id — один клиент = одна строка.
        """
        user_id = order.get("user_id")
        if user_id is None:
            return
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return
        chat_id = order.get("chat_id")
        if chat_id is not None:
            try:
                chat_id = int(chat_id)
            except (TypeError, ValueError):
                chat_id = None
        product = (order.get("product_code") or "").strip() or None
        self.upsert_client(user_id=user_id, chat_id=chat_id, product=product)

    def get_user_history(self, user_id: int) -> list[dict[str, str]]:
        """Возвращает историю диалога пользователя: список dict с ключами role, content."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT history_json FROM user_history WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return []
            try:
                data = json.loads(row[0] or "[]")
                return data if isinstance(data, list) else []
            except (TypeError, json.JSONDecodeError):
                return []
        finally:
            conn.close()

    def set_user_history(self, user_id: int, messages: list[dict[str, str]]) -> None:
        """Записывает историю диалога (список dict с role, content)."""
        now = int(time.time())
        raw = json.dumps(messages, ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO user_history (user_id, history_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET history_json = excluded.history_json, updated_at = excluded.updated_at
                """,
                (user_id, raw, now),
            )
        finally:
            conn.close()

    def append_user_message(
        self,
        user_id: int,
        role: str,
        content: str,
        max_pairs: int = 10,
    ) -> None:
        """Добавляет сообщение в историю и обрезает до max_pairs пар (user+assistant)."""
        messages = self.get_user_history(user_id)
        messages.append({"role": role, "content": content or ""})
        if max_pairs > 0:
            while len(messages) > max_pairs * 2:
                messages.pop(0)
        self.set_user_history(user_id, messages)

    def clear_user_history(self, user_id: int) -> None:
        """Очищает историю диалога пользователя."""
        self.set_user_history(user_id, [])

    def get_client(self, user_id: int) -> dict[str, Any] | None:
        """Возвращает запись клиента по user_id или None."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT user_id, chat_id, username, first_name, last_name, "
                "contact_channel, contact_value, profile_name, form_address, age_group, "
                "focus, duration, previous_attempts, conflict, self_value_scale, "
                "insight, readiness, product, tariff, preferred_contact_time, preferred_group_start, "
                "anket_json, updated_at FROM clients WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()


def _format_client_anket_table(client: dict[str, Any]) -> str:
    """Форматирует анкету клиента в читаемую таблицу для Telegram."""
    if not client:
        return "Анкета клиента отсутствует."
    lines = ["📋 Анкета клиента"]
    uid = client.get("user_id") or "—"
    lines.append(f"user_id: {uid}")
    if client.get("username"):
        lines.append(f"@username: {client.get('username')}")
    if client.get("first_name") or client.get("last_name"):
        name = " ".join(filter(None, [client.get("first_name"), client.get("last_name")]))
        lines.append(f"Имя: {name}")
    if client.get("form_address"):
        lines.append(f"Обращение: {client.get('form_address')}")
    if client.get("age_group"):
        lines.append(f"Возраст: {client.get('age_group')}")
    if client.get("contact_channel") or client.get("contact_value"):
        lines.append(f"Контакт: {client.get('contact_channel') or ''} — {client.get('contact_value') or ''}")
    if client.get("focus"):
        lines.append(f"Запрос: {client.get('focus')}")
    if client.get("duration"):
        lines.append(f"Длительность: {client.get('duration')}")
    if client.get("insight"):
        lines.append(f"Инсайт: {client.get('insight')}")
    if client.get("readiness"):
        lines.append(f"Готовность: {client.get('readiness')}")
    if client.get("product"):
        lines.append(f"Продукт: {client.get('product')}")
    if client.get("tariff"):
        lines.append(f"Тариф: {client.get('tariff')}")
    if client.get("preferred_contact_time"):
        lines.append(f"Удобное время: {client.get('preferred_contact_time')}")
    if client.get("preferred_group_start"):
        lines.append(f"Старт групповых: {client.get('preferred_group_start')}")
    return "\n".join(lines)


def build_payment_url(
    *,
    cfg: RobokassaConfig,
    inv_id: int,
    out_sum: str,
    description: str,
    shp: dict[str, str],
    email: str | None = None,
) -> str:
    out_sum_s = _to_amount_str(out_sum)
    sig_str = f"{cfg.merchant_login}:{out_sum_s}:{inv_id}:{cfg.password1}{_shp_signature_part(shp)}"
    signature = _md5_hex(sig_str)

    params: dict[str, str] = {
        "MerchantLogin": cfg.merchant_login,
        "OutSum": out_sum_s,
        "InvId": str(inv_id),
        "Description": description,
        "SignatureValue": signature,
        "Culture": "ru",
        "Encoding": "utf-8",
    }
    if cfg.is_test:
        params["IsTest"] = "1"
        # При IsTest=1 Робокасса принимает только ТЕСТОВУЮ пару паролей из раздела «Технические настройки».
        # Использование боевых паролей приводит к ошибке 29 и сообщению «Форма оплаты не работает».
        logging.getLogger(__name__).warning(
            "Robokassa: is_test=1. Убедитесь, что в .env указаны ТЕСТОВЫЕ Пароль №1 и Пароль №2 "
            "из вкладки «Технические настройки» личного кабинета Robokassa, а не боевые пароли."
        )
    if email:
        params["Email"] = email
    params.update(shp)
    return cfg.merchant_url + "?" + urlencode(params, doseq=True, safe=":/")


def verify_result_url(params: dict[str, Any], *, cfg: RobokassaConfig) -> dict[str, Any]:
    # Robokassa может прислать параметры в разном регистре — нормализуем.
    normalized: dict[str, Any] = {str(k): v for k, v in params.items()}

    out_sum = normalized.get("OutSum") or normalized.get("out_sum")
    inv_id = normalized.get("InvId") or normalized.get("inv_id")
    sig = normalized.get("SignatureValue") or normalized.get("signature_value")
    if out_sum is None or inv_id is None or sig is None:
        raise ValueError("Не хватает параметров OutSum/InvId/SignatureValue")

    # Для подписи используем OutSum в том виде, как прислала Робокасса (иначе не совпадёт).
    # Например, при OutSum=2990 они считают MD5 от "2990:...", а не от "2990.00:..."
    out_sum_raw = str(out_sum).strip()
    out_sum_s = _to_amount_str(out_sum_raw)
    inv_id_i = int(str(inv_id))
    shp = _extract_shp(normalized)

    # Строка для подписи: OutSum и InvId в том формате, как в запросе
    sig_str = f"{out_sum_raw}:{inv_id_i}:{cfg.password2}{_shp_signature_part(shp)}"
    expected = _md5_hex(sig_str)
    if str(sig).lower() != expected.lower():
        raise ValueError("Неверная подпись Robokassa (ResultURL)")

    return {
        "out_sum": out_sum_s,
        "inv_id": inv_id_i,
        "shp": shp,
        "signature_value": str(sig),
        "raw": normalized,
    }


def verify_success_url(params: dict[str, Any], *, cfg: RobokassaConfig) -> dict[str, Any]:
    normalized: dict[str, Any] = {str(k): v for k, v in params.items()}
    out_sum = normalized.get("OutSum") or normalized.get("out_sum")
    inv_id = normalized.get("InvId") or normalized.get("inv_id")
    sig = normalized.get("SignatureValue") or normalized.get("signature_value")
    if out_sum is None or inv_id is None or sig is None:
        raise ValueError("Не хватает параметров OutSum/InvId/SignatureValue")

    out_sum_s = _to_amount_str(str(out_sum))
    inv_id_i = int(str(inv_id))
    shp = _extract_shp(normalized)

    sig_str = f"{out_sum_s}:{inv_id_i}:{cfg.password1}{_shp_signature_part(shp)}"
    expected = _md5_hex(sig_str)
    if str(sig).lower() != expected.lower():
        raise ValueError("Неверная подпись Robokassa (SuccessURL)")

    return {
        "out_sum": out_sum_s,
        "inv_id": inv_id_i,
        "shp": shp,
        "signature_value": str(sig),
        "raw": normalized,
    }


def _parse_notify_chat_id(chat_id_str: str):
    """
    Парсит TELEGRAM_GROUP_NOTIFY_CHAT_ID: число (личный/группа) или @username (канал).
    Возвращает int (для chat_id) или str (для @channelusername).
    """
    s = (chat_id_str or "").strip()
    if not s:
        return None
    # Число (в т.ч. отрицательное для группы)
    if s.lstrip("-").isdigit():
        return int(s)
    # Имя канала/чата: с @ или без
    if not s.startswith("@"):
        s = "@" + s
    return s


def telegram_send_message(
    *,
    bot_token: str,
    chat_id: int | str,
    text: str,
    disable_web_preview: bool = False,
) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urlencode(
        {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true" if disable_web_preview else "false",
        }
    ).encode("utf-8")
    req = Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(req, timeout=10) as resp:
        _ = resp.read()


def build_access_message(product_code: str) -> str:
    """
    Возвращает текст, который бот отправит после подтверждения оплаты.
    Ссылки задаются переменными окружения; тон — благодарный и тёплый.
    """
    code = (product_code or "").strip()

    # Диагностическая консультация
    if code == "webinar":
        url = _env("DIAGNOSTIC_CONSULT_ACCESS_URL", _env("WEBINAR_ACCESS_URL", "")) or ""
        if url:
            return (
                "Ваша заявка на диагностическую консультацию подтверждена.\n\n"
                "Вот ссылка для записи или связи:\n"
                f"{url}\n\n"
                "Благодарю за доверие. Напишите в чат, и мы согласуем удобное время."
            )
        return (
            "Оплата диагностической консультации прошла успешно.\n\n"
            "Напишите в чат, и мы согласуем удобное время встречи. Благодарю за доверие!"
        )

    # Групповые занятия — от лица Владимы: благодарность и обещание связаться
    if code in ("group", "group_standard", "group_vip"):
        return (
            "Благодарю за оплату групповых занятий!\n\n"
            "Я очень ценю ваше доверие и рада, что вы со мной. "
            "В ближайшее время я свяжусь с вами для выбора удобного времени.\n\n"
            "Спасибо - вы часть моего сообщества! 💛"
        )

    # Платный бот / ИИ-психолог — от лица Владимы
    if code == "pro":
        url = _env("PRO_BOT_URL", "") or ""
        if url:
            return (
                "Оплата прошла успешно!\n\n"
                "Теперь у вас есть доступ к ИИ-психологу, обученному на базе моей многолетней практики "
                "и жизненного опыта — он создан, чтобы быть рядом в важные моменты.\n\n"
                f"Переходите по ссылке:\n{url}\n\n"
                "Рада видеть вас!"
            )
        return (
            "Оплата прошла успешно! Доступ к ИИ-психологу будет выдан в ближайшее время. "
            "Благодарю за доверие!"
        )

    return "Оплата прошла успешно. Благодарю за доверие!"


MSK = ZoneInfo("Europe/Moscow")


def process_result_url(
    params: dict[str, Any],
    *,
    cfg: RobokassaConfig,
    db: "PaymentsDB",
) -> tuple[bool, int]:
    """
    Общая логика ResultURL: проверка подписи, заказ, mark_paid, уведомление пользователю и в чат дайджеста.
    Возвращает (success, inv_id). success=True только если подпись верна, заказ найден, сумма/токен совпадают и ответ "OK".
    """
    log = logging.getLogger(__name__)
    try:
        parsed = verify_result_url(params, cfg=cfg)
    except Exception as e:
        log.warning("ResultURL: verify_result_url failed: %s", e)
        return (False, 0)

    inv_id = int(parsed["inv_id"])
    out_sum = str(parsed["out_sum"])
    order = db.get_order(inv_id)
    if not order:
        log.warning("ResultURL: unknown InvId=%s", inv_id)
        return (False, inv_id)
    if str(order.get("amount")) != out_sum:
        log.warning("ResultURL: amount mismatch InvId=%s %s != %s", inv_id, order.get("amount"), out_sum)
        return (False, inv_id)
    shp = parsed.get("shp") or {}
    token_expected = str(order.get("order_token") or "")
    token_got = str(shp.get("Shp_order_token") or "")
    if token_expected and token_got and token_expected != token_got:
        log.warning("ResultURL: token mismatch InvId=%s", inv_id)
        return (False, inv_id)

    newly_paid = db.mark_paid_if_pending(inv_id, raw_params=parsed.get("raw") or {})
    if newly_paid:
        order = db.get_order(inv_id)
        if order:
            try:
                db.upsert_client_from_order(order)
            except Exception as e:
                log.exception("ResultURL: upsert_client_from_order failed: %s", e)
        bot_token = (_env("TELEGRAM_BOT_TOKEN") or "").strip()
        if bot_token and order:
            chat_id = int(order.get("chat_id") or shp.get("Shp_chat_id") or 0)
            if chat_id:
                text = build_access_message(str(order.get("product_code") or ""))
                try:
                    telegram_send_message(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        text=text,
                        disable_web_preview=True,
                    )
                except Exception as e:
                    log.exception("ResultURL: Telegram sendMessage failed: %s", e)
            try:
                send_group_payment_notify_immediate(bot_token, db.get_order(inv_id), db=db)
            except Exception as e:
                log.exception("ResultURL: group digest immediate notify failed: %s", e)

    return (True, inv_id)


def send_group_payment_notify_immediate(bot_token: str, order: dict[str, Any], db: Optional["PaymentsDB"] = None) -> None:
    """
    Если GROUP_DIGEST_MODE=immediate и задан TELEGRAM_GROUP_NOTIFY_CHAT_ID,
    отправляет в чат дайджеста одну строку о только что оплаченном групповом заказе.
    db опционально — при отсутствии создаётся из env.
    """
    if (order.get("product_code") or "") not in ("group_standard", "group_vip"):
        return
    mode = (_env("GROUP_DIGEST_MODE") or "").strip().lower()
    if mode != "immediate":
        return
    chat_id_str = (_env("TELEGRAM_GROUP_NOTIFY_CHAT_ID") or "").strip()
    if not chat_id_str:
        return
    notify_chat_id = _parse_notify_chat_id(chat_id_str)
    if notify_chat_id is None:
        return
    paid_at = order.get("paid_at")
    if paid_at:
        dt = datetime.fromtimestamp(paid_at, tz=timezone.utc).astimezone(MSK)
        time_str = dt.strftime("%d.%m.%Y %H:%M")
    else:
        time_str = "—"
    user_id = order.get("user_id") or "—"
    chat_id = order.get("chat_id") or "—"
    product = (order.get("product_code") or "").replace("group_", "").capitalize()
    if product == "Standard":
        product = "Стандарт"
    elif product == "Vip":
        product = "VIP"
    amount = order.get("amount") or "—"
    text = f"Групповые (сразу): {time_str} МСК | user_id {user_id} | chat_id {chat_id} | {product} | {amount} ₽"
    try:
        telegram_send_message(bot_token=bot_token, chat_id=notify_chat_id, text=text)
        # Отправляем таблицу с анкетой клиента, если запись есть в clients.
        try:
            uid = order.get("user_id")
            if uid is not None:
                if db is None:
                    db = PaymentsDB.from_env()
                client = db.get_client(int(uid))
                if client:
                    anket_text = _format_client_anket_table(client)
                    if len(anket_text) > 4000:
                        anket_text = anket_text[:3997] + "..."
                    telegram_send_message(bot_token=bot_token, chat_id=notify_chat_id, text=anket_text)
                    if (os.getenv("DEBUG_ANKET_LOG") or "").strip().lower() in ("1", "true", "yes"):
                        try:
                            _dpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_anket.log")
                            with open(_dpath, "a", encoding="utf-8") as _df:
                                _df.write(json.dumps({"ts": time.time(), "message": "anket_sent_to_notify", "user_id": uid}, ensure_ascii=False) + "\n")
                        except Exception:
                            pass
        except Exception:
            logging.getLogger(__name__).exception("Групповой дайджест (immediate): не удалось отправить анкету клиента")
    except Exception:
        logging.getLogger(__name__).exception("Групповой дайджест (immediate): не удалось отправить в Telegram")
