# -*- coding: utf-8 -*-
"""
Обработчики для Robokassa под Yandex Cloud Functions (HTTP).

Сделано 3 точки входа (entrypoint):
  - deploy.handler_robokassa.handler_result  — ResultURL (server-to-server)
  - deploy.handler_robokassa.handler_success — SuccessURL (редирект пользователя)
  - deploy.handler_robokassa.handler_fail    — FailURL (редирект пользователя)

ResultURL ОБЯЗАТЕЛЕН: именно он подтверждает оплату. В ответ нужно вернуть "OK{InvId}".
"""

import base64
import json
import logging
import os
import sys
from urllib.parse import parse_qs

# Добавляем корень проекта в путь (как в handler_webhook.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robokassa_integration import (  # noqa: E402
    PaymentsDB,
    RobokassaConfig,
    verify_result_url,
    verify_success_url,
    process_result_url,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def _collect_params(event: dict) -> dict:
    params: dict = {}

    # query string
    q = event.get("queryStringParameters") or {}
    if isinstance(q, dict):
        params.update(q)

    # body может быть form-urlencoded
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    body = body.strip()
    if body:
        parsed = parse_qs(body, keep_blank_values=True)
        for k, v in parsed.items():
            if not v:
                continue
            params[k] = v[0]

    return params


def handler_result(event, context):
    """
    ResultURL (server-to-server). Должен вернуть "OK{InvId}".
    """
    try:
        cfg = RobokassaConfig.from_env()
        db = PaymentsDB.from_env()
        params = _collect_params(event)
        success, inv_id = process_result_url(params, cfg=cfg, db=db)
        body = f"OK{inv_id}" if success else "ERROR"
        return {"statusCode": 200, "body": body}
    except Exception as e:
        logging.exception("Robokassa ResultURL error: %s", e)
        return {"statusCode": 200, "body": "ERROR"}


def handler_success(event, context):
    """
    SuccessURL (редирект пользователя после оплаты).
    Это НЕ подтверждение оплаты, подтверждение приходит на ResultURL.
    """
    try:
        cfg = RobokassaConfig.from_env()
        params = _collect_params(event)
        _ = verify_success_url(params, cfg=cfg)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": "Оплата принята. Вернитесь в Telegram — бот пришлёт доступ.",
        }
    except Exception as e:
        logging.exception("Robokassa SuccessURL error: %s", e)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": "Не удалось проверить оплату. Если деньги списались — напишите в поддержку.",
        }


def handler_fail(event, context):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
        "body": "Оплата не завершена. Вы можете попробовать ещё раз в боте.",
    }

