#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Печатает URL для кабинета Robokassa из PUBLIC_BASE_URL в .env (рядом с этим файлом)."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
except ImportError:
    print("Установите python-dotenv: pip install python-dotenv", file=sys.stderr)
    sys.exit(1)

load_dotenv(os.path.join(_ROOT, ".env"))
base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
if not base:
    print(
        "PUBLIC_BASE_URL не задан в .env. Пример:\n"
        "  PUBLIC_BASE_URL=https://denis-komkov-robokassa-server.duckdns.org",
        file=sys.stderr,
    )
    sys.exit(1)

print("Скопируйте в личный кабинет Robokassa (технические URL):")
print()
print(f"Result URL:  {base}/robokassa/result")
print(f"Success URL: {base}/robokassa/success")
print(f"Fail URL:    {base}/robokassa/fail")
print()
print("Мини-приложение (для проверки / для кнопки в боте): PUBLIC_BASE_URL → …/miniapp задаётся в коде бота.")
