import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncpg

# Ініціалізація логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Зчитування змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Критична помилка: Відсутні обов'язкові змінні оточення BOT_TOKEN, BASE_URL або DATABASE_URL!")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

DB_POOL = None
ADMIN_ID = 124303561  # Твій ID розробника

# ==========================================
# РОБОТА З БАЗОЮ ДАНИХ (asyncpg)
# ==========================================

async def get_db_connection():
    global DB_POOL
    if DB_POOL is None:
        try:
            DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
            logger.info("Пул підключень до БД успішно створено.")
        except Exception as e:
            logger.error(f"Помилка створення пулу підключень до БД: {e}")
            raise e
    return DB_POOL

async def init_db():
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        # Створення таблиць, якщо їх немає
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS games (
                chat_id BIGINT PRIMARY KEY,
                status TEXT DEFAULT 'registration',
                round_number INT DEFAULT 0,
                players JSONB DEFAULT '{}'::jsonb,
                current_word_data JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pro_users (
                user_id BIGINT PRIMARY KEY,
                is_pro BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        # Автоматичний апгрейд структури для база на Supabase
        await conn.execute('''
            ALTER TABLE games ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
        ''')
        await conn.execute('''
            ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
        ''')
        await conn.execute('''
            ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
        ''')
        
        # Заповнюємо порожні значення created_at для старих записів, щоб працювала статистика
        await conn.execute('''
            UPDATE games SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;
        ''')
        await conn.execute('''
            UPDATE pro_users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;
        ''')
        await conn.execute('''
            UPDATE pro_users SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL;
        ''')
        
        logger.info("Таблиці в БД перевірено та успішно оновлено.")

async def load_game(chat_id: int):
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, round_number, players, current_word_data FROM games WHERE chat_id = $1", chat_id)
        if row:
            return {
                "status": row["status"],
                "round_number": row["round_number"],
                "players": json.loads(row["players"]) if isinstance(row["players"], str) else row["players"],
                "current_word_data": json.loads(row["current_word_data"]) if row["current_word_data"] else {}
            }
        return None

async def save_game(chat_id: int, status: str, round_number: int, players: dict, current_word_data: dict = None):
    pool = await get_db_connection()
    players_json = json.dumps(players)
    current_word_json = json.dumps(current_word_data) if current_word_data else "{}"
    
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO games (chat_id, status, round_number, players, current_word_data)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) 
            DO UPDATE SET status = $2, round_number = $3, players = $4, current_word_data = $5
        ''', chat_id, status, round_number, players_json, current_word_json)

async def is_user_pro(user_id: int) -> bool:
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT is_pro FROM pro_users WHERE user_id = $1", user_id)
        return bool(val)

async def set_user_pro_status(user_id: int, status: bool):
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO pro_users (user_id, is_pro, created_at, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET is_pro = $2, updated_at = CURRENT_TIMESTAMP
        ''', user_id, status)

async def check_group_has_pro(chat_id: int) -> bool:
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        pro_rows = await conn.fetch("SELECT user_id FROM pro_users WHERE is_pro = true")
        pro_user_ids = [row["user_id"] for row in pro_rows]
        
        if await is_user_pro(ADMIN_ID) and ADMIN_ID not in pro_user_ids:
            pro_user_ids.append(ADMIN_ID)
            
        for u_id in pro_user_ids:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=u_id)
                if member.status in ["creator", "administrator", "member"]:
                    return True
            except Exception:
                continue
    return False

async def get_chat_players_count(chat_id: int) -> int:
    try:
        count = await bot.get_chat_member_count(chat_id)
        return count
    except Exception as e:
        logger.error(f"Помилка отримання кількості учасників: {e}")
        return 0

async def filter_active_players(chat_id: int, players: dict, current_word_data: dict) -> (dict, dict):
    active_players = {}
    was_changed = False
    for p_id, p_info in players.items():
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=int(p_id))
            if member.status not in ["left", "kicked"]:
                active_players[p_id] = p_info
            else:
                was_changed = True
        except Exception:
            active_players[p_id] = p_info
            
    if was_changed:
        if not current_word_data:
            current_word_data = {}
        current_word_data["composition_changed"] = True
        
    return active_players, current_word_data

async def check_and_handle_alone(chat_id: int, callback: types.CallbackQuery = None) -> bool:
    count = await get_chat_players_count(chat_id)
    actual_humans = count - 1 if count > 0 else 1
    
    if actual_humans < 2:
        text = (
            "Щоб грати, додайте в групу другого гравця.\n\n"
            "Щоб перезапустити бота, напишіть в чат команду /start або /play."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
        ])
        try:
            if callback:
                await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
                await callback.answer()
            else:
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Помилка відправки повідомлення про соло-гру: {e}")
        return True
    return False

def generate_initial_scoreboard(players: dict) -> str:
    if not players:
        return "player 1: 0\nplayer 2: 0"
    
    lines = [f"{p['name']}: {p.get('score', 0)}" for p in players.values()]
    if len(lines) == 1:
        lines.append("player 2: 0")
    return "\n".join(lines)

async def get_db_stats_isolated(pool, dt=None):
    time_filter = "AND created_at >= $1" if dt else ""
    params = [dt] if dt else []

    not_only_admin_filter = f"""
        (players = '{{}}'::jsonb OR NOT (
            players ? '{ADMIN_ID}' AND (SELECT count(*) FROM jsonb_object_keys(players)) = 1
        ))
    """

    sql_chats = f"SELECT COUNT(*) FROM games WHERE {not_only_admin_filter} {time_filter}"
    sql_games_10 = f"SELECT COUNT(*) FROM games WHERE (status='playing_free' OR (status='finished' AND round_number=10) OR (status='registration' AND round_number=0)) AND {not_only_admin_filter} {time_filter}"
    sql_games_100 = f"SELECT COUNT(*) FROM games WHERE (status='playing_pro' OR (status='finished' AND round_number=100)) AND {not_only_admin_filter} {time_filter}"

    sql_users = f"""
        SELECT COUNT(DISTINCT user_id) FROM (
            SELECT jsonb_object_keys(players)::bigint AS user_id FROM games 
            WHERE {not_only_admin_filter} {time_filter}
        ) AS all_users
    """
    sql_pro = f"""
        SELECT COUNT(DISTINCT user_id) FROM (
            SELECT jsonb_object_keys(players)::bigint AS user_id FROM games 
            WHERE {not_only_admin_filter} {time_filter}
        ) AS active_users
        JOIN pro_users ON pro_users.user_id = active_users.user_id WHERE pro_users.is_pro = true
    """

    async with pool.acquire() as conn:
        chats = await conn.fetchval(sql_chats, *params)
        games_10 = await conn.fetchval(sql_games_10, *params)
        games_100 = await conn.fetchval(sql_games_100, *params)
        users = await conn.fetchval(sql_users, *params)
        pro = await conn.fetchval(sql_pro, *params)
        
        # Надійна підстраховка: якщо значень немає (база пуста), повертаємо 0
        chats = chats if chats is not None else 0
        games_10 = games_10 if games_10 is not None else 0
        games_100 = games_100 if games_100 is not None else 0
        users = users if users is not None else 0
        pro = pro if pro is not None else 0
        free = users - pro if users >= pro else 0

    return chats, games_10, games_100, users, free, pro

# ==========================================
# ЛОГІКА ХЕНДЛЕРІВ
# ==========================================

# 1. КОМАНДИ АДМІНІСТРАТОРА (Вручну в приватних повідомленнях)

@dp.message(F.chat.type == "private", Command("free", "pro"))
async def toggle_admin_status(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        command = message.text.split()[0].replace("/", "").lower()
        if "pro" in command:
            await set_user_pro_status(ADMIN_ID, True)
            await message.reply("Твій статус Pro")
        else:
            await set_user_pro_status(ADMIN_ID, False)
            await message.reply("Твій статус free")

@dp.message(F.chat.type == "private", Command("stat"))
async def admin_stat(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        pool = await get_db_connection()
        now = datetime.now()
        
        res0, res1, res2, res3, res4 = await asyncio.gather(
            get_db_stats_isolated(pool),
            get_db_stats_isolated(pool, now - timedelta(days=365)),
            get_db_stats_isolated(pool, now - timedelta(days=30)),
            get_db_stats_isolated(pool, now - timedelta(days=7)),
            get_db_stats_isolated(pool, now - timedelta(hours=24))
        )

        stat_text = (
            f"ЗА ВЕСЬ ЧАС:\n"
            f"- всі чати: {res0[0]}\n"
            f"- всі ігри до 10: {res0[1]}\n"
            f"- всі ігри до 100: {res0[2]}\n"
            f"- всі юзери: {res0[3]}\n"
            f"- free-юзери: {res0[4]}\n"
            f"- pro-юзери: {res0[5]}\n\n"
            f"ПРИРІСТ ЗА РІК:\n"
            f"- всі чати: +{res1[0]}\n"
            f"- всі ігри до 10: +{res1[1]}\n"
            f"- всі ігри до 100: +{res1[2]}\n"
            f"- всі юзери: +{res1[3]}\n"
            f"- free-юзери: +{res1[4]}\n"
            f"- pro-юзери: +{res1[5]}\n\n"
            f"ПРИРІСТ ЗА 30 ДНІВ:\n"
            f"- всі чати: +{res2[0]}\n"
            f"- всі ігри до 10: +{res2[1]}\n"
            f"- всі ігри до 100: +{res2[2]}\n"
            f"- всі юзери: +{res2[3]}\n"
            f"- free-юзери: +{res2[4]}\n"
            f"- pro-юзери: +{res2[5]}\n\n"
            f"ПРИРІСТ ЗА 7 ДНІВ:\n"
            f"- всі чати: +{res3[0]}\n"
            f"- всі ігри до 10: +{res3[1]}\n"
            f"- всі ігри до 100: +{res3[2]}\n"
            f"- всі юзери: +{res3[3]}\n"
            f"- free-юзери: +{res3[4]}\n"
            f"- pro-юзери: +{res3[5]}\n\n"
            f"ПРИРІСТ ЗА 24 ГОД:\n"
            f"- всі чати: +{res4[0]}\n"
            f"- всі ігри до 10: +{res4[1]}\n"
            f"- всі ігри до 100: +{res4[2]}\n"
            f"- всі юзери: +{res4[3]}\n"
            f"- free-юзери: +{res4[4]}\n"
            f"- pro-юзери: +{res4[5]}"
        )
        await message.answer(stat_text)

# 2. ПОСТ-ЗАГЛУШКА (Для приватних повідомлень)

@dp.message(F.chat.type == "private")
async def private_stub(message: types.Message):
    # Повне блокування будь-яких повідомлень, тексту чи старту в приваті, якщо пише адмін
    if message.from_user.id == ADMIN_ID:
        return
        
    # Для всіх інших користувачів видається чітка текстова заглушка
    text = "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). Знайдеш мене по пошуку @stofotobot"
    await message.answer(text)

# 3. ЛОГІКА ДЛЯ РОБОТИ У ГРУПАХ

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added_to_group(event: types.ChatMemberUpdated):
    chat_id = event.chat.id
    await save_game(chat_id, "registration", 0, {})
    try:
        await show_rules_or_limits(chat_id)
    except Exception as e:
        logger.error(f"Помилка відображення правил при додаванні: {e}")

@dp.message(Command("start", "play"))
async def manual_start_in_group(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        chat_id = message.chat.id
        
        existing_game = await load_game(chat_id)
        players = existing_game["players"] if existing_game and "players" in existing_game else {}
        current_word_data = existing_game["current_word_data"] if existing_game and "current_word_data" in existing_game else {}
        
        players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
        
        for p_id in players:
            players[p_id]["score"] = 0
            
        await save_game(chat_id, "registration", 0, players, current_word_data)
        try:
            await show_rules_or_limits(chat_id)
        except Exception as e:
            logger.error(f"Помилка у manual_start_in_group: {e}")

async def show_rules_or_limits(chat_id: int):
    count = await get_chat_players_count(chat_id)
    actual_humans = count - 1 if count > 0 else 1
    has_pro = await check_group_has_pro(chat_id)

    if has_pro:
        if actual_humans == 1:
            await bot.send_message(chat_id=chat_id, text="1 людина в групі")
            return
        elif actual_humans > 10:
            await bot.send_message(chat_id=chat_id, text="11 людей в групі")
            return
    else:
        if actual_humans == 1:
            await bot.send_message(chat_id=chat_id, text="1 людина в групі")
            return
        elif actual_humans >= 3:
            await bot.send_message(chat_id=chat_id, text="3 людини в групі")
            return

    text = (
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у чат. Хто перший – отримує 1 бал.\n\n"
        "2. Кожен раунд = 1 photo / 1 бал. Безоплатна гра триває 10 раундів, платна – 100.\n\n"
        "3. Не можна викладати числа предметами чи писати самому. Можна лише фотографувати їх вдома, на вулиці тощо.\n\n"
        "4. Не можна брати двічі числа з однієї локації (сторінки книги, кнопки ліфту тощо). Локації мають бути різними.\n\n"
        "5. Якщо надіслане фото не відповідає завданню, його можна відмінити і почати раунд заново.\n\n"
        "Бот реагує лише на фото і кнопки, тож можете вільно спілкуватись у чаті.\n\n"
        "Щоб перезапустити бота, напишіть /start або /play.\n\n"
        "Придумайте приз і гоу!"
    )

    if has_pro:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="start_pro_buy")],
            [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="start_pro_buy")]
        ])
        
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_web_page_preview=True)

@dp.callback_query(F.data == "start_free_10")
async def start_free_game(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    
    if await check_and_handle_alone(chat_id, callback):
        return
        
    game = await load_game(chat_id)
    players = game["players"] if game and "players" in game else {}
    current_word_data = game["current_word_data"] if game and "current_word_data" in game else {}
    
    players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
    
    creator_id = str(callback.from_user.id)
    creator_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
    if creator_id not in players:
        players[creator_id] = {"name": creator_name, "score": 0}

    for p_id in players:
        players[p_id]["score"] = 0
        
    current_word_data["number"] = 1
    await save_game(chat_id, "playing_free", 1, players, current_word_data)
    
    scoreboard = generate_initial_scoreboard(players)
    
    text = (
        "Раунд 1.\n\n"
        "Рахунок\n"
        f"{scoreboard}\n\n"
        "Завдання: сфотографуй число 1."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
    ])
    await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "start_pro_game_active")
async def start_pro_game_active(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    
    if await check_and_handle_alone(chat_id, callback):
        return
        
    game = await load_game(chat_id)
    players = game["players"] if game and "players" in game else {}
    current_word_data = game["current_word_data"] if game and "current_word_data" in game else {}
    
    players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
    
    creator_id = str(callback.from_user.id)
    creator_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
    if creator_id not in players:
        players[creator_id] = {"name": creator_name, "score": 0}

    for p_id in players:
        players[p_id]["score"] = 0
        
    current_word_data["number"] = 1
    await save_game(chat_id, "playing_pro", 1, players, current_word_data)
    
    scoreboard = generate_initial_scoreboard(players)
    
    text = (
        "Раунд 1.\n\n"
        "Рахунок\n"
        f"{scoreboard}\n\n"
        "Завдання: сфотографуй число 1."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
    ])
    await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "start_pro_buy")
async def show_pro_payment(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    
    if await check_and_handle_alone(chat_id, callback):
        return
        
    if await is_user_pro(user_id):
        game = await load_game(chat_id)
        players = game["players"] if game and "players" in game else {}
        current_word_data = game["current_word_data"] if game and "current_word_data" in game else {}
        
        players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
        
        creator_id = str(callback.from_user.id)
        creator_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
        if creator_id not in players:
            players[creator_id] = {"name": creator_name, "score": 0}

        for p_id in players:
            players[p_id]["score"] = 0
            
        current_word_data["number"] = 1
        await save_game(chat_id, "playing_pro", 1, players, current_word_data)
        
        scoreboard = generate_initial_scoreboard(players)
        
        text = (
            "Раунд 1.\n\n"
            "Рахунок\n"
            f"{scoreboard}\n\n"
            "Завдання: сфотографуй число 1."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
        ])
        await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        await callback.answer()
        return

    mono_link = f"https://send.monobank.ua/jar/8Sg7bYg9Xb?a=100&m={user_id}"
    text = (
        "Pro-версія гри:\n"
        "- до 10 гравців\n"
        "- до 100 раундів назавжди\n"
        "- у всіх чатах Pro-гравця"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=mono_link)],
        [InlineKeyboardButton(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="start_free_10")]
    ])
    await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("clear_round_"))
async def clear_round_handler(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    
    if await check_and_handle_alone(chat_id, callback):
        return
        
    game = await load_game(chat_id)
    if not game or game["status"] not in ["playing_free", "playing_pro", "finished"]:
        await callback.answer()
        return
        
    try:
        target_round = int(callback.data.replace("clear_round_", ""))
    except ValueError:
        await callback.answer()
        return

    if target_round < 1:
        target_round = 1

    players = game["players"]
    current_word_data = game["current_word_data"] if game and "current_word_data" in game else {}
    
    players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
    
    user_id_to_decrement = str(callback.from_user.id)
    if user_id_to_decrement in players:
        if players[user_id_to_decrement]["score"] > 0:
            players[user_id_to_decrement]["score"] -= 1

    if target_round == 100:
        current_status = "playing_pro"
    elif target_round == 10:
        current_status = "playing_free"
    else:
        current_status = "playing_pro" if game["status"] == "playing_pro" else "playing_free"

    current_word_data["number"] = target_round
    await save_game(chat_id, current_status, target_round, players, current_word_data)
    
    lines = [f"{p['name']}: {p['score']}" for p in players.values()]
    if len(lines) == 0:
        scoreboard = "player 1: 0\nplayer 2: 0"
    elif len(lines) == 1:
        scoreboard = f"{lines[0]}\nplayer 2: 0"
    else:
        scoreboard = "\n".join(lines)
    
    if target_round == 1:
        text = (
            f"Раунд 1.\n\n"
            f"Рахунок\n"
            f"{scoreboard}\n\n"
            f"Завдання: сфотографуй число 1."
        )
        if current_status == "playing_free":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
            ])
    else:
        text = (
            f"Раунд {target_round}\n\n"
            f"Рахунок\n"
            f"{scoreboard}\n\n"
            f"Завдання: число {target_round}"
        )
        if current_status == "playing_free":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {target_round - 1}", callback_data=f"clear_round_{target_round - 1}")],
                [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {target_round - 1}", callback_data=f"clear_round_{target_round - 1}")],
                [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
            ])
        
    await callback.message.answer(text=text, reply_markup=kb)
    await callback.answer()

@dp.message(F.chat.type.in_(["group", "supergroup"]) & F.photo)
async def handle_game_photo(message: types.Message):
    chat_id = message.chat.id
    game = await load_game(chat_id)
    
    if not game or game["status"] not in ["playing_free", "playing_pro"]:
        return

    round_num = game["round_number"]
    players = game["players"]
    current_word_data = game["current_word_data"] if game and "current_word_data" in game else {}
    
    players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
    
    user_id = str(message.from_user.id)
    u_name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name

    if user_id not in players:
        if game["status"] == "playing_free":
            if len(players) >= 2:
                if not await check_group_has_pro(chat_id):
                    text = (
                        "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n\n"
                        "Pro-версія гри:\n"
                        "- до 10 гравців\n"
                        "- до 100 раундів назавжди\n"
                        "- у всіх чатах Pro-гравця"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="start_pro_buy")]
                    ])
                    await message.reply(text, reply_markup=kb)
                    return
        elif game["status"] == "playing_pro":
            if len(players) >= 10:
                text = (
                    "На жаль, грати може максимум 10 гравців.\n\n"
                    "Щоб перезапустити бота, напишіть в чат команду /start або /play."
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="noop")]
                ])
                await message.reply(text, reply_markup=kb)
                return

        current_word_data["composition_changed"] = True
        players[user_id] = {"name": u_name, "score": 0}
        
    players[user_id]["score"] += 1
    max_rounds = 10 if game["status"] == "playing_free" else 100
    
    if round_num >= max_rounds:
        lines = [f"{p['name']}: {p['score']}" for p in players.values()]
        if len(lines) == 1:
            lines.append("player 2: 0")
        scoreboard = "\n".join(lines)
        
        if game["status"] == "playing_free":
            text = (
                f"Переможець: {u_name}\n\n"
                f"Рахунок\n"
                f"{scoreboard}\n\n"
                f"Не забудь про свій приз!"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="ОБНУЛИТИ РАУНД 10", callback_data="clear_round_10")],
                [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
                [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="start_pro_buy")],
                [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="start_pro_buy")]
            ])
        else:
            text = (
                f"Переможець: {u_name}\n\n"
                f"Рахунок\n"
                f"{scoreboard}\n\n"
                f"Не забудь \nпро свій приз!"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="ОБНУЛИТИ РАУНД 100", callback_data="clear_round_100")],
                [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
            ])
            
        if current_word_data.get("composition_changed"):
            next_status = "registration"
            current_word_data = {}  
        else:
            next_status = "finished"
            
        await save_game(chat_id, next_status, round_num, players, current_word_data)
        await message.answer(text, reply_markup=kb)
        return

    next_round = round_num + 1
    current_word_data["number"] = next_round
    await save_game(chat_id, game["status"], next_round, players, current_word_data)

    lines = [f"{p['name']}: {p['score']}" for p in players.values()]
    if len(lines) == 1:
        lines.append("player 2: 0")
    scoreboard = "\n".join(lines)
    
    if game["status"] == "playing_free":
        text = (
            f"Раунд {next_round}\n\n"
            f"Рахунок\n"
            f"{scoreboard}\n\n"
            f"Завдання: число {next_round}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {next_round - 1}", callback_data=f"clear_round_{next_round - 1}")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
        ])
    else:
        text = (
            f"Раунд {next_round}\n\n"
            f"Рахунок\n"
            f"{scoreboard}\n\n"
            f"Завдання: число {next_round}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {next_round - 1}", callback_data=f"clear_round_{next_round - 1}")],
            [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
        ])
        
    await message.answer(text, reply_markup=kb)

# ==========================================
# FastAPI та МОНІТОРИНГ ОПЛАТ
# ==========================================

app = FastAPI()

@app.post("/webhook")
async def bot_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
    
    try:
        update = types.Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Помилка при обробці апдейту: {e}")
        
    return Response(status_code=200)

@app.post("/mono_webhook")
async def mono_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
        
    if data.get("type") == "StatementItem":
        statement = data.get("data", {}).get("statementItem", {})
        comment = statement.get("comment", "")
        amount = statement.get("amount", 0)
        
        if amount >= 10000:
            user_id = None
            words = comment.split()
            for word in words:
                if word.isdigit() and len(word) >= 7:
                    user_id = int(word)
                    break
                    
            if user_id:
                await set_user_pro_status(user_id, True)
                try:
                    user_row = await bot.get_chat(user_id)
                    u_name = f"@{user_row.username}" if user_row.username else user_row.first_name
                    
                    text = (
                        "Дякую, оплата є!\n\n"
                        f"– {u_name} тепер Pro\n"
                        "– відкрито 100 раундів\n"
                        "– відкрито 10 гравців"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_pro_game_active")]
                    ])
                    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати сповіщення в чат користувачу: {e}")
                    
    return Response(status_code=200)

@app.get("/")
async def root():
    return JSONResponse(content={"status": "working", "bot": "100_photo_bot"}, headers={"Content-Type": "application/json; charset=utf-8"})

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    logger.info("Вебхук успішно встановлено!")
    
    yield
    
    logger.info("Закриття додатка, очищення ресурсів...")
    await dp.storage.close()
    
    if bot.session:
        await bot.session.close()
        logger.info("Сесію бота успішно закрито.")
        
    if DB_POOL:
        await DB_POOL.close()
        logger.info("Пул підключень до БД успішно закрито.")

app.router.lifespan_context = lifespan
