import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import ChatMemberUpdatedFilter, Command, JOIN_TRANSITION
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stofotobot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Відсутні обов'язкові змінні оточення BOT_TOKEN, BASE_URL або DATABASE_URL")

ADMIN_ID = 124303561
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
MONO_PAYMENT_BASE_URL = os.getenv("MONO_PAYMENT_BASE_URL") or "https://send.monobank.ua/jar/8Sg7bYg9Xb"
PRO_PRICE_KOPIYKY = 10000

STATUS_REGISTRATION = "registration"
STATUS_PLAYING = "playing"
STATUS_FINISHED = "finished"
MODE_FREE = "free"
MODE_PRO = "pro"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_POOL: asyncpg.Pool | None = None


TEXT_PRIVATE_STUB = (
    "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). "
    "Знайдеш мене по пошуку @stofotobot"
)

TEXT_ONE_PERSON = (
    "Щоб грати, додайте в групу другого гравця.\n\n"
    "Щоб перезапустити бота, напишіть в чат команду /start або /play."
)

TEXT_THREE_PEOPLE = (
    "Щоб грати втрьох і більше, хоч 1 гравець має бути pro.\n\n"
    "Pro-версія гри:\n"
    "- до 10 гравців\n"
    "- до 100 раундів назавжди\n"
    "- у всіх чатах pro-гравця"
)

TEXT_ELEVEN_PEOPLE = (
    "Грати може максимум 10 людей.\n\n"
    "Щоб перезапустити бота, напишіть в чат команду /start або /play."
)

TEXT_RULES = (
    "Правила гри:\n\n"
    "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у чат. Хто перший – отримує 1 бал.\n\n"
    "2. Кожен раунд = 1 фото / 1 бал. Безоплатна гра триває 10 раундів, платна – 100.\n\n"
    "3. Числа не можна писати чи викладати предметами. Можна лише фотографувати їх вдома, на вулиці тощо.\n\n"
    "4. Не можна брати двічі числа з однієї локації (сторінки книги, кнопки ліфту тощо). Локації мають бути різними.\n\n"
    "5. Якщо надіслане фото не відповідає завданню, його можна відмінити і почати раунд заново.\n\n"
    "Бот реагує лише на фото і кнопки, тож можете вільно спілкуватись у чаті.\n\n"
    "Щоб перезапустити бота, напишіть /start або /play.\n\n"
    "Придумайте приз і гоу!"
)

TEXT_PAYMENT = (
    "Pro-версія гри:\n"
    "- до 10 гравців\n"
    "- до 100 раундів назавжди\n"
    "- у всіх чатах pro-гравця"
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
