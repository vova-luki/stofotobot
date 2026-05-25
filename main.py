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
    return value


def user_display_name(user: types.User) -> str:
    name = user.full_name or ""
    if not name.strip() and user.username:
        name = f"@{user.username}"
    if not name.strip():
        name = f"user {user.id}"
    return " ".join(name.split())


def sorted_players(players: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return sorted(players.items(), key=lambda item: (int(item[1].get("order", 10_000)), int(item[0])))


def ensure_player(players: dict[str, dict[str, Any]], user: types.User) -> dict[str, dict[str, Any]]:
    user_id = str(user.id)
    current_orders = [int(player.get("order", 0)) for player in players.values() if str(player.get("order", "")).isdigit()]
    next_order = max(current_orders, default=0) + 1
    existing = players.get(user_id, {})
    players[user_id] = {
        "name": user_display_name(user),
        "score": int(existing.get("score", 0)),
        "order": int(existing.get("order", next_order)),
        "seen_at": now_utc().isoformat(),
    }
    return players


def reset_scores(players: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for player in players.values():
        player["score"] = 0
    return players


def scoreboard(players: dict[str, dict[str, Any]], min_slots: int = 2) -> str:
    lines = [f"{player['name']}: {int(player.get('score', 0))}" for _, player in sorted_players(players)]
    while len(lines) < min_slots:
        lines.append(f"player {len(lines) + 1}: 0")
    return "\n".join(lines)


def max_rounds(mode: str) -> int:
    return 100 if mode == MODE_PRO else 10


def round_text(round_number: int, players: dict[str, dict[str, Any]]) -> str:
    task = "Завдання: сфотографуй число 1" if round_number == 1 else f"Завдання: число {round_number}"
    return f"Раунд {round_number}\n\nРахунок\n{scoreboard(players)}\n\n{task}"


def final_text(winner_name: str, players: dict[str, dict[str, Any]]) -> str:
    return f"Переможець: {winner_name}\n\nРахунок\n{scoreboard(players)}\n\nНе забудь про свій приз!"


def rules_keyboard(has_pro: bool) -> InlineKeyboardMarkup:
    if has_pro:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro")]]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="payment")],
            [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="payment")],
        ]
    )


def one_person_keyboard(has_pro: bool) -> InlineKeyboardMarkup:
    text = "НОВА ГРА" if has_pro else "НОВА ГРА ДО 10"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data="restart")]])


def three_people_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="payment")],
            [InlineKeyboardButton(text="НАС ВЖЕ ДВОЄ", callback_data="restart")],
        ]
    )


def eleven_people_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="restart")]])


def round_keyboard(mode: str, round_number: int, allow_clear: bool) -> InlineKeyboardMarkup | None:
    if round_number == 1 and not allow_clear:
        return None
    buttons: list[list[InlineKeyboardButton]] = []
    if allow_clear and round_number > 1:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"ОБНУЛИТИ РАУНД {round_number - 1}",
                    callback_data=f"clear:{round_number - 1}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="НОВА ГРА" if mode == MODE_PRO else "НОВА ГРА ДО 10", callback_data="restart")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def final_keyboard(mode: str) -> InlineKeyboardMarkup:
    if mode == MODE_PRO:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="ОБНУЛИТИ РАУНД 100", callback_data="clear:100")],
                [InlineKeyboardButton(text="НОВА ГРА", callback_data="restart")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ОБНУЛИТИ РАУНД 10", callback_data="clear:10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="restart")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="payment")],
            [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="payment")],
        ]
    )


def payment_keyboard(user_id: int) -> InlineKeyboardMarkup:
    query = urlencode({"a": 100, "m": str(user_id)})
    separator = "&" if "?" in MONO_PAYMENT_BASE_URL else "?"
    payment_url = f"{MONO_PAYMENT_BASE_URL}{separator}{query}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=payment_url)],
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="restart")],
        ]
    )


def payment_success_text(user_name: str) -> str:
    return (
        "Дякую, оплата є!\n\n"
        f"– {user_name} тепер pro\n"
        "– відкрито 100 раундів\n"
        "– відкрито 10 гравців"
    )


def payment_success_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="НОВА ГРА", callback_data="restart")]])


async def get_db_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            statement_cache_size=0,
        )
        logger.info("Database pool created")
    return DB_POOL


async def init_db() -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                is_pro BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS is_pro BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE chats SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute("UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                username TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute("UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pro_users (
                user_id BIGINT PRIMARY KEY,
                is_pro BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute("ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS is_pro BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE pro_users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute("UPDATE pro_users SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_game_sessions (
                chat_id BIGINT PRIMARY KEY,
                status TEXT DEFAULT 'registration',
                mode TEXT,
                round_number INT DEFAULT 0,
                players JSONB DEFAULT '{}'::jsonb,
                last_photo_user_id BIGINT,
                current_game_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'registration';")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS mode TEXT;")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS round_number INT DEFAULT 0;")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS players JSONB DEFAULT '{}'::jsonb;")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS last_photo_user_id BIGINT;")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS current_game_id BIGINT;")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE bot_game_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE bot_game_sessions SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute("UPDATE bot_game_sessions SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_game_history (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                mode TEXT NOT NULL,
                players JSONB DEFAULT '{}'::jsonb,
                created_by BIGINT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute("ALTER TABLE bot_game_history ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        await conn.execute("ALTER TABLE bot_game_history ADD COLUMN IF NOT EXISTS mode TEXT;")
        await conn.execute("ALTER TABLE bot_game_history ADD COLUMN IF NOT EXISTS players JSONB DEFAULT '{}'::jsonb;")
        await conn.execute("ALTER TABLE bot_game_history ADD COLUMN IF NOT EXISTS created_by BIGINT;")
        await conn.execute("ALTER TABLE bot_game_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE bot_game_history SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_clicks (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute("ALTER TABLE payment_clicks ADD COLUMN IF NOT EXISTS user_id BIGINT;")
        await conn.execute("ALTER TABLE payment_clicks ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        await conn.execute("ALTER TABLE payment_clicks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE payment_clicks SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        logger.info("Database schema checked")


async def safe_send_message(chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as exc:
        logger.warning("Telegram send failed for chat %s: %s", chat_id, exc)
    except Exception as exc:
        logger.exception("Unexpected send failure for chat %s: %s", chat_id, exc)
    return False


async def safe_answer_message(message: types.Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await message.answer(text=text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as exc:
        logger.warning("Telegram answer failed in chat %s: %s", message.chat.id, exc)
    except Exception as exc:
        logger.exception("Unexpected answer failure in chat %s: %s", message.chat.id, exc)
    return False


async def safe_callback_answer(callback: types.CallbackQuery, text: str | None = None) -> None:
    try:
        await callback.answer(text)
    except TelegramAPIError as exc:
        logger.warning("Callback answer failed: %s", exc)


async def upsert_chat(chat_id: int, is_pro: bool | None = None) -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        if is_pro is None:
            await conn.execute(
                """
                INSERT INTO chats (chat_id, updated_at)
                VALUES ($1, CURRENT_TIMESTAMP)
                ON CONFLICT (chat_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                """,
                chat_id,
            )
        else:
            await conn.execute(
                """
                INSERT INTO chats (chat_id, is_pro, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (chat_id) DO UPDATE SET is_pro = $2, updated_at = CURRENT_TIMESTAMP
                """,
                chat_id,
                is_pro,
            )


async def upsert_user(user: types.User) -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, name, username, updated_at)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id)
            DO UPDATE SET name = $2, username = $3, updated_at = CURRENT_TIMESTAMP
            """,
            user.id,
            user_display_name(user),
            user.username,
        )


async def load_session(chat_id: int) -> dict[str, Any]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT chat_id, status, mode, round_number, players, last_photo_user_id, current_game_id, created_at
            FROM bot_game_sessions
            WHERE chat_id = $1
            """,
            chat_id,
        )
    if not row:
        return {
            "chat_id": chat_id,
            "status": STATUS_REGISTRATION,
            "mode": None,
            "round_number": 0,
            "players": {},
            "last_photo_user_id": None,
            "current_game_id": None,
        }
    return {
        "chat_id": row["chat_id"],
        "status": row["status"],
        "mode": row["mode"],
        "round_number": int(row["round_number"] or 0),
        "players": load_json(row["players"], {}),
        "last_photo_user_id": row["last_photo_user_id"],
        "current_game_id": row["current_game_id"],
        "created_at": row["created_at"],
    }


async def save_session(
    chat_id: int,
    status: str,
    mode: str | None,
    round_number: int,
    players: dict[str, dict[str, Any]],
    last_photo_user_id: int | None = None,
    current_game_id: int | None = None,
) -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_game_sessions
                (chat_id, status, mode, round_number, players, last_photo_user_id, current_game_id, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, CURRENT_TIMESTAMP)
            ON CONFLICT (chat_id) DO UPDATE SET
                status = $2,
                mode = $3,
                round_number = $4,
                players = $5::jsonb,
                last_photo_user_id = $6,
                current_game_id = $7,
                updated_at = CURRENT_TIMESTAMP
            """,
            chat_id,
            status,
            mode,
            round_number,
            json.dumps(players, ensure_ascii=False),
            last_photo_user_id,
            current_game_id,
        )
    await upsert_chat(chat_id, is_pro=(mode == MODE_PRO if mode else None))


async def create_game_history(chat_id: int, mode: str, players: dict[str, dict[str, Any]], created_by: int) -> int:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        game_id = await conn.fetchval(
            """
            INSERT INTO bot_game_history (chat_id, mode, players, created_by)
            VALUES ($1, $2, $3::jsonb, $4)
            RETURNING id
            """,
            chat_id,
            mode,
            json.dumps(players, ensure_ascii=False),
            created_by,
        )
    return int(game_id)


async def update_game_history_players(game_id: int | None, players: dict[str, dict[str, Any]]) -> None:
    if not game_id:
        return
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE bot_game_history SET players = $2::jsonb WHERE id = $1",
            game_id,
            json.dumps(players, ensure_ascii=False),
        )


async def set_user_pro_status(user_id: int, is_pro: bool) -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pro_users (user_id, is_pro, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id)
            DO UPDATE SET is_pro = $2, updated_at = CURRENT_TIMESTAMP
            """,
            user_id,
            is_pro,
        )


async def is_user_pro(user_id: int) -> bool:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT is_pro FROM pro_users WHERE user_id = $1", user_id)
    return bool(value)


async def group_has_pro(chat_id: int) -> bool:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM pro_users WHERE is_pro = TRUE")
    for row in rows:
        user_id = row["user_id"]
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in {"creator", "administrator", "member"}:
                await upsert_chat(chat_id, is_pro=True)
                return True
        except TelegramAPIError:
            continue
    await upsert_chat(chat_id, is_pro=False)
    return False


async def actual_human_count(chat_id: int) -> int:
    try:
        total = await bot.get_chat_member_count(chat_id)
        return max(total - 1, 0)
    except TelegramAPIError as exc:
        logger.warning("Could not count chat members for %s: %s", chat_id, exc)
    except Exception as exc:
        logger.exception("Unexpected member count failure for %s: %s", chat_id, exc)
    return 0


async def remember_message_user(message: types.Message) -> None:
    if message.chat.type not in {"group", "supergroup"} or not message.from_user:
        return
    await upsert_user(message.from_user)
    session = await load_session(message.chat.id)
    players = session["players"]
    players = ensure_player(players, message.from_user)
    await save_session(
        message.chat.id,
        session["status"],
        session["mode"],
        session["round_number"],
        players,
        session["last_photo_user_id"],
        session["current_game_id"],
    )
    await update_game_history_players(session["current_game_id"], players)


async def remember_callback_user(callback: types.CallbackQuery) -> dict[str, Any]:
    if not callback.message or callback.message.chat.type not in {"group", "supergroup"}:
        return {}
    await upsert_user(callback.from_user)
    session = await load_session(callback.message.chat.id)
    players = ensure_player(session["players"], callback.from_user)
    session["players"] = players
    await save_session(
        callback.message.chat.id,
        session["status"],
        session["mode"],
        session["round_number"],
        players,
        session["last_photo_user_id"],
        session["current_game_id"],
    )
    await update_game_history_players(session["current_game_id"], players)
    return session


async def reset_to_registration(chat_id: int) -> dict[str, Any]:
    session = await load_session(chat_id)
    players = reset_scores(session["players"])
    await save_session(chat_id, STATUS_REGISTRATION, session["mode"], 0, players, None, session["current_game_id"])
    session.update({"status": STATUS_REGISTRATION, "round_number": 0, "players": players, "last_photo_user_id": None})
    return session


async def send_gate_post(chat_id: int, reset: bool = False) -> None:
    await upsert_chat(chat_id)
    humans = await actual_human_count(chat_id)
    has_pro = await group_has_pro(chat_id)

    if humans < 2:
        await safe_send_message(chat_id, TEXT_ONE_PERSON, one_person_keyboard(has_pro))
        return
    if has_pro and humans >= 11:
        await safe_send_message(chat_id, TEXT_ELEVEN_PEOPLE, eleven_people_keyboard())
        return
    if not has_pro and humans >= 3:
        await safe_send_message(chat_id, TEXT_THREE_PEOPLE, three_people_keyboard())
        return

    session = await load_session(chat_id)
    if reset or session["status"] != STATUS_PLAYING:
        await save_session(chat_id, STATUS_REGISTRATION, MODE_PRO if has_pro else MODE_FREE, 0, session["players"], None, None)
        await safe_send_message(chat_id, TEXT_RULES, rules_keyboard(has_pro))
        return

    await safe_send_message(
        chat_id,
        round_text(session["round_number"], session["players"]),
        round_keyboard(session["mode"], session["round_number"], allow_clear=bool(session["last_photo_user_id"])),
    )


async def start_game(chat_id: int, user: types.User, requested_mode: str) -> None:
    humans = await actual_human_count(chat_id)
    has_pro = await group_has_pro(chat_id)
    mode = MODE_PRO if has_pro or requested_mode == MODE_PRO else MODE_FREE

    if humans < 2:
        await safe_send_message(chat_id, TEXT_ONE_PERSON, one_person_keyboard(has_pro))
        return
    if mode == MODE_FREE and humans >= 3:
        await safe_send_message(chat_id, TEXT_THREE_PEOPLE, three_people_keyboard())
        return
    if mode == MODE_PRO and humans >= 11:
        await safe_send_message(chat_id, TEXT_ELEVEN_PEOPLE, eleven_people_keyboard())
        return

    session = await load_session(chat_id)
    players = ensure_player(reset_scores(session["players"]), user)
    game_id = await create_game_history(chat_id, mode, players, user.id)
    await save_session(chat_id, STATUS_PLAYING, mode, 1, players, None, game_id)
    await safe_send_message(chat_id, round_text(1, players), None)


async def send_payment_post(chat_id: int, user: types.User) -> None:
    if await is_user_pro(user.id):
        await set_user_pro_status(user.id, True)
        await upsert_chat(chat_id, is_pro=True)
        await safe_send_message(chat_id, payment_success_text(user_display_name(user)), payment_success_keyboard())
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO payment_clicks (user_id, chat_id) VALUES ($1, $2)", user.id, chat_id)
    await safe_send_message(chat_id, TEXT_PAYMENT, payment_keyboard(user.id))


async def send_stats(message: types.Message) -> None:
    pool = await get_db_pool()
    periods = [
        ("ЗА ВЕСЬ ЧАС", None),
        ("ПРИРІСТ ЗА РІК", now_utc() - timedelta(days=365)),
        ("ПРИРІСТ ЗА 30 ДНІВ", now_utc() - timedelta(days=30)),
        ("ПРИРІСТ ЗА 7 ДНІВ", now_utc() - timedelta(days=7)),
        ("ПРИРІСТ ЗА 24 ГОД", now_utc() - timedelta(hours=24)),
    ]

    async def period_stats(since: datetime | None) -> tuple[int, int, int, int, int, int]:
        date_clause_sessions = "AND created_at >= $2" if since else ""
        date_clause_history = "AND created_at >= $2" if since else ""
        date_clause_users = "AND created_at >= $2" if since else ""
        params = [str(ADMIN_ID)]
        if since:
            params.append(since)

        async with pool.acquire() as conn:
            chats = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM bot_game_sessions
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_object_keys(players) AS p(user_id)
                    WHERE p.user_id <> $1
                )
                {date_clause_sessions}
                """,
                *params,
            )
            games_free = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM bot_game_history
                WHERE mode = 'free'
                AND EXISTS (
                    SELECT 1 FROM jsonb_object_keys(players) AS p(user_id)
                    WHERE p.user_id <> $1
                )
                {date_clause_history}
                """,
                *params,
            )
            games_pro = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM bot_game_history
                WHERE mode = 'pro'
                AND EXISTS (
                    SELECT 1 FROM jsonb_object_keys(players) AS p(user_id)
                    WHERE p.user_id <> $1
                )
                {date_clause_history}
                """,
                *params,
            )
            users = await conn.fetchval(
                f"SELECT COUNT(*) FROM users WHERE user_id <> $1 {date_clause_users}",
                *params,
            )
            pro_users = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM users
                JOIN pro_users USING (user_id)
                WHERE users.user_id <> $1
                AND pro_users.is_pro = TRUE
                {date_clause_users}
                """,
                *params,
            )
        users = users or 0
        pro_users = pro_users or 0
        return chats or 0, games_free or 0, games_pro or 0, users, max(users - pro_users, 0), pro_users

    results = await asyncio.gather(*(period_stats(since) for _, since in periods))
    blocks = []
    for index, ((title, _), values) in enumerate(zip(periods, results)):
        prefix = "+" if index else ""
        blocks.append(
            f"{title}:\n"
            f"- всі чати: {prefix}{values[0]}\n"
            f"- всі ігри до 10: {prefix}{values[1]}\n"
            f"- всі ігри до 100: {prefix}{values[2]}\n"
            f"- всі юзери: {prefix}{values[3]}\n"
            f"- free-юзери: {prefix}{values[4]}\n"
            f"- pro-юзери: {prefix}{values[5]}"
        )
    await safe_answer_message(message, "\n\n".join(blocks))


@dp.message(F.chat.type == "private", Command("stat"))
async def admin_stat(message: types.Message) -> None:
    if message.from_user and message.from_user.id == ADMIN_ID:
        await send_stats(message)


@dp.message(F.chat.type == "private", Command("free", "pro"))
async def admin_toggle_pro(message: types.Message) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    command = (message.text or "").split()[0].lower()
    is_pro = command == "/pro"
    await set_user_pro_status(ADMIN_ID, is_pro)
    await safe_answer_message(message, "Твій статус Pro" if is_pro else "Твій статус free")


@dp.message(F.chat.type == "private")
async def private_stub(message: types.Message) -> None:
    if message.from_user and message.from_user.id == ADMIN_ID:
        return
    await safe_answer_message(message, TEXT_PRIVATE_STUB)


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added_to_group(event: types.ChatMemberUpdated) -> None:
    if event.chat.type not in {"group", "supergroup"}:
        return
    await asyncio.sleep(1)
    await send_gate_post(event.chat.id, reset=True)


@dp.chat_member()
async def member_changed(event: types.ChatMemberUpdated) -> None:
    if event.chat.type not in {"group", "supergroup"}:
        return
    if event.new_chat_member.status in {"member", "administrator", "creator"}:
        humans = await actual_human_count(event.chat.id)
        has_pro = await group_has_pro(event.chat.id)
        if (not has_pro and humans >= 3) or (has_pro and humans >= 11):
            await send_gate_post(event.chat.id, reset=False)


@dp.message(F.chat.type.in_({"group", "supergroup"}), Command("start", "play"))
async def start_or_play(message: types.Message) -> None:
    await remember_message_user(message)
    session = await reset_to_registration(message.chat.id)
    await save_session(message.chat.id, STATUS_REGISTRATION, session["mode"], 0, session["players"], None, None)
    await send_gate_post(message.chat.id, reset=True)


@dp.callback_query(F.message.chat.type == "private")
async def private_callback(callback: types.CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID and callback.message:
        await safe_send_message(callback.message.chat.id, TEXT_PRIVATE_STUB)
    await safe_callback_answer(callback)


@dp.callback_query(F.message.chat.type.in_({"group", "supergroup"}) & F.data == "restart")
async def restart_from_button(callback: types.CallbackQuery) -> None:
    await remember_callback_user(callback)
    session = await reset_to_registration(callback.message.chat.id)
    await save_session(callback.message.chat.id, STATUS_REGISTRATION, session["mode"], 0, session["players"], None, None)
    await send_gate_post(callback.message.chat.id, reset=True)
    await safe_callback_answer(callback)


@dp.callback_query(F.message.chat.type.in_({"group", "supergroup"}) & F.data == "start_free")
async def start_free(callback: types.CallbackQuery) -> None:
    await remember_callback_user(callback)
    await start_game(callback.message.chat.id, callback.from_user, MODE_FREE)
    await safe_callback_answer(callback)


@dp.callback_query(F.message.chat.type.in_({"group", "supergroup"}) & F.data == "start_pro")
async def start_pro(callback: types.CallbackQuery) -> None:
    await remember_callback_user(callback)
    await start_game(callback.message.chat.id, callback.from_user, MODE_PRO)
    await safe_callback_answer(callback)


@dp.callback_query(F.message.chat.type.in_({"group", "supergroup"}) & F.data == "payment")
async def payment(callback: types.CallbackQuery) -> None:
    await remember_callback_user(callback)
    if await actual_human_count(callback.message.chat.id) < 2:
        await safe_send_message(callback.message.chat.id, TEXT_ONE_PERSON, one_person_keyboard(False))
    else:
        await send_payment_post(callback.message.chat.id, callback.from_user)
    await safe_callback_answer(callback)


@dp.callback_query(F.message.chat.type.in_({"group", "supergroup"}) & F.data.startswith("clear:"))
async def clear_round(callback: types.CallbackQuery) -> None:
    await remember_callback_user(callback)
    session = await load_session(callback.message.chat.id)
    if session["status"] not in {STATUS_PLAYING, STATUS_FINISHED} or session["mode"] not in {MODE_FREE, MODE_PRO}:
        await safe_callback_answer(callback)
        return

    try:
        target_round = max(1, int((callback.data or "").split(":", 1)[1]))
    except (IndexError, ValueError):
        await safe_callback_answer(callback)
        return

    players = session["players"]
    last_user_id = str(session["last_photo_user_id"]) if session["last_photo_user_id"] else None
    if last_user_id and last_user_id in players:
        players[last_user_id]["score"] = max(int(players[last_user_id].get("score", 0)) - 1, 0)

    await save_session(
        callback.message.chat.id,
        STATUS_PLAYING,
        session["mode"],
        target_round,
        players,
        None,
        session["current_game_id"],
    )
    await update_game_history_players(session["current_game_id"], players)
    await safe_send_message(callback.message.chat.id, round_text(target_round, players), round_keyboard(session["mode"], target_round, False))
    await safe_callback_answer(callback)


@dp.message(F.chat.type.in_({"group", "supergroup"}) & F.photo)
async def handle_photo(message: types.Message) -> None:
    if not message.from_user:
        return
    await remember_message_user(message)
    session = await load_session(message.chat.id)
    if session["status"] != STATUS_PLAYING or session["mode"] not in {MODE_FREE, MODE_PRO}:
        return

    humans = await actual_human_count(message.chat.id)
    has_pro = await group_has_pro(message.chat.id)
    if session["mode"] == MODE_FREE and not has_pro and humans >= 3:
        await safe_answer_message(message, TEXT_THREE_PEOPLE, three_people_keyboard())
        return
    if (session["mode"] == MODE_PRO or has_pro) and humans >= 11:
        await safe_answer_message(message, TEXT_ELEVEN_PEOPLE, eleven_people_keyboard())
        return

    players = ensure_player(session["players"], message.from_user)
    user_id = str(message.from_user.id)
    players[user_id]["score"] = int(players[user_id].get("score", 0)) + 1
    round_number = session["round_number"]
    mode = session["mode"]
    limit = max_rounds(mode)

    if round_number >= limit:
        await save_session(message.chat.id, STATUS_FINISHED, mode, round_number, players, message.from_user.id, session["current_game_id"])
        await update_game_history_players(session["current_game_id"], players)
        await safe_answer_message(message, final_text(user_display_name(message.from_user), players), final_keyboard(mode))
        return

    next_round = round_number + 1
    await save_session(message.chat.id, STATUS_PLAYING, mode, next_round, players, message.from_user.id, session["current_game_id"])
    await update_game_history_players(session["current_game_id"], players)
    await safe_answer_message(message, round_text(next_round, players), round_keyboard(mode, next_round, True))


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def remember_any_group_activity(message: types.Message) -> None:
    await remember_message_user(message)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    await init_db()
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(
            WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
    logger.info("Webhook is ready: %s", WEBHOOK_URL)
    try:
        yield
    finally:
        await dp.storage.close()
        await bot.session.close()
        if DB_POOL is not None:
            await DB_POOL.close()
        logger.info("Application stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"status": "working", "bot": "100_photo_bot"})


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    try:
        data = await request.json()
        update = types.Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as exc:
        logger.exception("Telegram update processing failed: %s", exc)
    return Response(status_code=200)


@app.post("/mono_webhook")
async def mono_webhook(request: Request) -> Response:
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    if data.get("type") != "StatementItem":
        return Response(status_code=200)

    statement = data.get("data", {}).get("statementItem", {})
    amount = int(statement.get("amount") or 0)
    comment = str(statement.get("comment") or "")
    if amount < PRO_PRICE_KOPIYKY:
        return Response(status_code=200)

    user_id = next((int(word) for word in comment.replace(",", " ").split() if word.isdigit() and len(word) >= 7), None)
    if not user_id:
        return Response(status_code=200)

    await set_user_pro_status(user_id, True)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        payment_row = await conn.fetchrow(
            """
            SELECT chat_id
            FROM payment_clicks
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
        )

    user_name = f"@{user_id}"
    try:
        chat = await bot.get_chat(user_id)
        if chat.full_name:
            user_name = " ".join(chat.full_name.split())
    except TelegramAPIError:
        pass

    if payment_row:
        chat_id = payment_row["chat_id"]
        await upsert_chat(chat_id, is_pro=True)
        await safe_send_message(chat_id, payment_success_text(user_name), payment_success_keyboard())
    await safe_send_message(user_id, payment_success_text(user_name), payment_success_keyboard())
    return Response(status_code=200)
