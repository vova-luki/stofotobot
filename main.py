import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
import asyncpg

# Ініціалізація логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Зчитування змінних оточення (без дефолтних значень)
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Критична помилка: Відсутні обов'язкові змінні оточення BOT_TOKEN, BASE_URL або DATABASE_URL!")

# Налаштування ендпоінту для Вебхука
WEBHOOK_PATH = f"/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# Ініціалізація бота та диспетчера
bot = Bot(
    token=BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

# Глобальний пул підключень до БД
db_pool = None

# Адміністратор системи
ADMIN_ID = 124303561

# --- ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ РОБОТИ З БД ---

async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL)
    return db_pool

async def get_chat_pro_status(chat_id: int) -> bool:
    """Перевіряє, чи є в чаті PRO-користувачі або чи активований PRO для чату."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        game = await conn.fetchrow("SELECT status, players FROM games WHERE chat_id = $1", chat_id)
        if game and game['status'] == 'PRO':
            return True
        
        if game and game['players']:
            players = json.loads(game['players'])
            if players:
                user_ids = [int(uid) for uid in players.keys() if uid.isdigit()]
                if user_ids:
                    pro_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM users WHERE telegram_id = ANY($1) AND is_pro = TRUE", 
                        user_ids
                    )
                    if pro_count > 0:
                        return True
    return False

async def get_user_pro_status(user_id: int) -> bool:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        is_pro = await conn.fetchval("SELECT is_pro FROM users WHERE telegram_id = $1", user_id)
        return bool(is_pro)

async def set_user_pro_status(user_id: int, is_pro: bool):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, is_pro, updated_at) 
            VALUES ($1, $2, NOW()) 
            ON CONFLICT (telegram_id) DO UPDATE SET is_pro = $2, updated_at = NOW()
            """, user_id, is_pro
        )

async def init_or_get_game(chat_id: int) -> dict:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, current_round, players, last_photo_by WHERE chat_id = $1", chat_id)
        if not row:
            default_players = {}
            await conn.execute(
                """
                INSERT INTO games (chat_id, status, current_round, players, created_at, updated_at)
                VALUES ($1, 'FREE', 0, $2, NOW(), NOW())
                """, chat_id, json.dumps(default_players)
            )
            return {"status": "FREE", "current_round": 0, "players": default_players, "last_photo_by": None}
        return {
            "status": row['status'],
            "current_round": row['current_round'],
            "players": json.loads(row['players'] or '{}'),
            "last_photo_by": row['last_photo_by']
        }

async def save_game(chat_id: int, status: str, current_round: int, players: dict, last_photo_by: int = None):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE games 
            SET status = $2, current_round = $3, players = $4, last_photo_by = $5, updated_at = NOW()
            WHERE chat_id = $1
            """, chat_id, status, current_round, json.dumps(players), last_photo_by
        )

async def update_user_in_db(user: types.User):
    pool = await get_db_pool()
    name = user.first_name
    if user.username:
        name = f"@{user.username}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (telegram_id) DO UPDATE SET username = $2, updated_at = NOW()
            """, user.id, user.username or ''
        )
    return name

# --- ФОНОВА ЛОГІКА ОНОВЛЕННЯ ІМЕН З БУДЬ-ЯКОЇ АКТИВНОСТІ ---

async def update_player_name_background(chat_id: int, user: types.User):
    if user.is_bot:
        return
    
    real_name = user.first_name
    if user.username:
        real_name = f"@{user.username}"
        
    await update_user_in_db(user)
    
    game = await init_or_get_game(chat_id)
    players = game["players"]
    user_id_str = str(user.id)
    
    if user_id_str in players:
        if players[user_id_str].get("name") != real_name:
            players[user_id_str]["name"] = real_name
            await save_game(chat_id, game["status"], game["current_round"], players, game["last_photo_by"])
    else:
        players[user_id_str] = {"name": real_name, "score": 0}
        await save_game(chat_id, game["status"], game["current_round"], players, game["last_photo_by"])

# --- ФОРМУВАННЯ ТЕКСТІВ ТА ШАБЛОНІВ ПОСТІВ ---

def format_scoreboard(players: dict, max_slots: int = 2) -> str:
    lines = []
    active_players = list(players.items())
    
    # Показуємо або реальних гравців, або заповнюємо пусті слоти за замовчуванням
    slots_to_show = max(max_slots, len(active_players))
    
    for i in range(slots_to_show):
        if i < len(active_players):
            uid, pdata = active_players[i]
            lines.append(f"{pdata['name']}: {pdata['score']}")
        else:
            lines.append(f"player {i+1}: 0")
            
    return "\n".join(lines)

async def send_welcome_rules(chat_id: int):
    is_pro = await get_chat_pro_status(chat_id)
    
    text = (
        'Вітаємо у <a href="https://t.me/stophotobot">100 PHOTO</a>!\n'
        'Правила гри:\n\n'
        '1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат. 1 раунд = 1 photo.\n\n'
        '2. За кожне photo гравець отримує 1 бал. Безоплатна гра триває 10 раундів, платна – 100 раундів.\n\n'
        '3. Числа не можна створювати (викладати предметами) або писати самому. Лише фотографувати їх вдома, на вулиці тощо.\n\n'
        '4. Не можна брати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо).\n'
        'Локації мають бути різними.\n\n'
        '5. Якщо надіслане photo не відповідає правилам, це photo можна відмінити і почати раунд заново.\n'
        'Щоб перезапустити бота, напишіть у чат команду /start або /play.\n\n'
        'За бажанням, придумайте приз переможцю.\n\n'
        'Натхнення!'
    )
    
    if is_pro:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_new_game")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="go_to_payment")],
            [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="go_to_payment")]
        ])
        
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_web_page_preview=False)
        return True
    except TelegramAPIError as e:
        logger.error(f"Помилка відправки правил в чат {chat_id}: {e}")
        return False

# --- ОБРОБКА КОМАНД ТА ЗАГЛУШОК У ЛІЧЦІ ---

@dp.message(F.chat.type == "private")
async def private_chat_handler(message: types.Message):
    if message.from_user.id == ADMIN_ID and message.text == "/stat":
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            all_chats = await conn.fetchval("SELECT COUNT(*) FROM games")
            all_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            pro_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE")
            free_users = all_users - pro_users
            
            now = datetime.utcnow()
            def_query = "SELECT COUNT(*) FROM games WHERE created_at >= $1"
            
            chats_24h = await conn.fetchval(def_query, now - timedelta(days=1))
            users_24h = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", now - timedelta(days=1))
            pro_24h = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND updated_at >= $1", now - timedelta(days=1))
            
            chats_7d = await conn.fetchval(def_query, now - timedelta(days=7))
            users_7d = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", now - timedelta(days=7))
            pro_7d = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND updated_at >= $1", now - timedelta(days=7))
            
            chats_30d = await conn.fetchval(def_query, now - timedelta(days=30))
            users_30d = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", now - timedelta(days=30))
            pro_30d = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND updated_at >= $1", now - timedelta(days=30))
            
            chats_1y = await conn.fetchval(def_query, now - timedelta(days=365))
            users_1y = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", now - timedelta(days=365))
            pro_1y = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND updated_at >= $1", now - timedelta(days=365))

        stat_text = (
            f"ЗА ВЕСЬ ЧАС:\n"
            f"- всі чати: {all_chats}\n"
            f"- всі юзери: {all_users}\n"
            f"- free-юзери: {free_users}\n"
            f"- pro-юзери: {pro_users}\n\n"
            f"ПРИРІСТ ЗА РІК:\n"
            f"- всі чати: +{chats_1y}\n"
            f"- всі юзери: +{users_1y}\n"
            f"- free-юзери: +{users_1y - pro_1y}\n"
            f"- pro-юзери: +{pro_1y}\n\n"
            f"ПРИРІСТ ЗА 30 ДНІВ:\n"
            f"- всі чати: +{chats_30d}\n"
            f"- всі юзери: +{users_30d}\n"
            f"- free-юзери: +{users_30d - pro_30d}\n"
            f"- pro-юзери: +{pro_30d}\n\n"
            f"ПРИРІСТ ЗА 7 ДНІВ:\n"
            f"- всі чати: +{chats_7d}\n"
            f"- всі юзери: +{users_7d}\n"
            f"- free-юзери: +{users_7d - pro_7d}\n"
            f"- pro-юзери: +{pro_7d}\n\n"
            f"ПРИРІСТ ЗА 24 ГОД:\n"
            f"- всі чати: +{chats_24h}\n"
            f"- всі юзери: +{users_24h}\n"
            f"- free-юзери: +{users_24h - pro_24h}\n"
            f"- pro-юзери: +{pro_24h}"
        )
        await message.answer(stat_text)
        return

    text = (
        "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу).\n"
        "Знайдеш мене через пошук – @stophotobot"
    )
    await message.answer(text)

# --- МОНІТОР ДОДАВАННЯ БОТА В ГРУПИ ---

@dp.my_chat_member(ChatMemberUpdatedFilter(member_change=JOIN_TRANSITION))
async def bot_added_to_group(event: types.ChatMemberUpdated):
    chat_id = event.chat.id
    try:
        await init_or_get_game(chat_id)
        count = await event.chat.get_member_count()
        players_count = count - 1
        
        is_pro = await get_chat_pro_status(chat_id)
        
        if is_pro:
            if players_count == 1:
                await bot.send_message(chat_id, "Щоб грати, додайте в групу другого гравця.\n"
                                                "Щоб перезапустити бота, напишіть в чат команду /start або /play.",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                           [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
                                       ]))
            elif 2 <= players_count <= 10:
                await send_welcome_rules(chat_id)
            else:
                await bot.send_message(chat_id, "На жаль, грати може максимум 10 гравців.\n"
                                                "Щоб перезапустити бота, напишіть в чат команду /start або /play.",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                           [InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="check_10_players")]
                                       ]))
        else:
            if players_count == 1:
                await bot.send_message(chat_id, "Щоб грати, додайте в групу другого гравця.\n"
                                                "Щоб перезапустити бота, напишіть в чат команду /start або /play.",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                           [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
                                       ]))
            elif players_count == 2:
                await send_welcome_rules(chat_id)
            else:
                await bot.send_message(chat_id, "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n"
                                                "Pro-версія гри:\n"
                                                "- до 10 гравців\n"
                                                "- до 100 раундів назавжди\n"
                                                "- у всіх чатах Pro-гравця",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                           [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="go_to_payment")]
                                       ]))
    except TelegramAPIError as e:
        logger.error(f"Помилка при додаванні бота в чат {chat_id}: {e}")

# --- ОБРОБКА КОМАНД ПЕРЕЗАПУСКУ У ГРУПАХ ---

@dp.message(F.chat.type.in_({"group", "supergroup"}), Command("start", "play"))
async def reset_game_command(message: types.Message):
    chat_id = message.chat.id
    await update_player_name_background(chat_id, message.from_user)
    
    game = await init_or_get_game(chat_id)
    players = game["players"]
    for uid in players:
        players[uid]["score"] = 0
        
    await save_game(chat_id, game["status"], 0, players, None)
    await send_welcome_rules(chat_id)

# --- ПЕРЕХОПЛЕННЯ ТЕКСТІВ ТА СМАЙЛІВ ДЛЯ ОНОВЛЕННЯ НІКНЕЙМІВ ---

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def background_text_catcher(message: types.Message):
    await update_player_name_background(message.chat.id, message.from_user)

# --- ОБРОБКА CALLBACK КНОПОК (ЛОГІКА ГРИ) ---

@dp.callback_query(F.data == "start_free_10")
async def process_start_free_10(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await update_player_name_background(chat_id, callback.from_user)
    
    game = await init_or_get_game(chat_id)
    players = game["players"]
    
    await save_game(chat_id, "FREE", 1, players, None)
    score_text = format_scoreboard(players, max_slots=2)
    
    text = (
        f"Раунд 1.\n\n"
        f"Рахунок\n"
        f"{score_text}\n\n"
        f"Завдання: сфотографуй число 1."
    )
    
    try:
        await callback.message.answer(text)
        await callback.answer()
    except TelegramAPIError as e:
        logger.error(f"Error: {e}")

@dp.callback_query(F.data == "start_new_game")
async def process_start_new_game_pro(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await update_player_name_background(chat_id, callback.from_user)
    
    game = await init_or_get_game(chat_id)
    players = game["players"]
    
    await save_game(chat_id, "PRO", 1, players, None)
    slots = max(2, len(players))
    score_text = format_scoreboard(players, max_slots=slots)
    
    text = (
        f"Раунд 1.\n\n"
        f"Рахунок\n"
        f"{score_text}\n\n"
        f"Завдання: сфотографуй число 1."
    )
    try:
        await callback.message.answer(text)
        await callback.answer()
    except TelegramAPIError as e:
        logger.error(f"Error: {e}")

@dp.callback_query(F.data == "go_to_payment")
async def process_payment_post(callback: types.CallbackQuery):
    text = (
        f"Pro-версія гри:\n"
        f"- до 10 гравців\n"
        f"- до 100 раундів назавжди\n"
        f"- у всіх чатах Pro-гравця"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url="https://send.monobank.ua/YOUR_PRO_LINK")],
        [InlineKeyboardButton(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="start_free_10")]
    ])
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("undo_round_"))
async def process_undo_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await update_player_name_background(chat_id, callback.from_user)
    
    target_round = int(callback.data.split("_")[-1])
    game = await init_or_get_game(chat_id)
    players = game["players"]
    last_photo_by = game["last_photo_by"]
    
    if last_photo_by and str(last_photo_by) in players:
        if players[str(last_photo_by)]["score"] > 0:
            players[str(last_photo_by)]["score"] -= 1
            
    await save_game(chat_id, game["status"], target_round, players, None)
    
    is_pro = (game["status"] == "PRO")
    slots = max(2, len(players)) if is_pro else 2
    score_text = format_scoreboard(players, max_slots=slots)
    
    if target_round == 1:
        text = (
            f"Раунд 1.\n\n"
            f"Рахунок\n"
            f"{score_text}\n\n"
            f"Завдання: сфотографуй число 1."
        )
        kb = None
    else:
        text = (
            f"Раунд {target_round}\n\n"
            f"Рахунок\n"
            f"{score_text}\n\n"
            f"Завдання: число {target_round}"
        )
        kb_text = "НОВА ГРА" if is_pro else "НОВА ГРА ДО 10"
        kb_data = "start_new_game" if is_pro else "start_free_10"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {target_round-1}", callback_data=f"undo_round_{target_round-1}")],
            [InlineKeyboardButton(text=kb_text, callback_data=kb_data)]
        ])
        
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

# --- ОБРОБКА PHOTO (ОСНОВНИЙ ІГРОВИЙ ПРОЦЕС) ---

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
async def process_game_photo(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    sender_name = await update_user_in_db(message.from_user)
    await update_player_name_background(chat_id, message.from_user)
    
    game = await init_or_get_game(chat_id)
    current_round = game["current_round"]
    players = game["players"]
    status = game["status"]
    
    if current_round == 0:
        return
        
    is_pro = (status == "PRO")
    max_rounds = 100 if is_pro else 10
    
    count = await message.chat.get_member_count()
    actual_chat_members = count - 1
    
    # Сувора перевірка лімітів кількості учасників у чаті на базі поточного статусу гри
    if not is_pro and actual_chat_members >= 3:
        await message.answer(
            "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n"
            "Pro-версія гри:\n"
            "- до 10 гравців\n"
            "- до 100 раундів назавжди\n"
            "- у всіх чатах Pro-гравця",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="go_to_payment")]
            ])
        )
        return
    elif is_pro and actual_chat_members >= 11:
        await message.answer(
            "На жаль, грати може максимум 10 гравців.\n"
            "Щоб перезапустити бота, напишіть в чат команду /start або /play.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="check_10_players")]
            ])
        )
        return

    user_id_str = str(user_id)
    if user_id_str not in players:
        players[user_id_str] = {"name": sender_name, "score": 0}
        
    players[user_id_str]["score"] += 1
    
    if current_round == max_rounds:
        winner_id = max(players, key=lambda k: players[k]["score"])
        winner_name = players[winner_id]["name"]
        
        slots = max(2, len(players)) if is_pro else 2
        score_text = format_scoreboard(players, max_slots=slots)
        
        text = (
            f"Переможець: {winner_name}\n\n"
            f"Рахунок\n"
            f"{score_text}\n\n"
            f"Не забудь про свій приз!"
        )
        
        if is_pro:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {max_rounds}", callback_data=f"undo_round_{max_rounds}")],
                [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_new_game")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД 10", callback_data="undo_round_10")],
                [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
                [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="go_to_payment")],
                [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="go_to_payment")]
            ])
            
        await save_game(chat_id, status, 0, players, user_id)
        await message.answer(text, reply_markup=kb)
    else:
        next_round = current_round + 1
        await save_game(chat_id, status, next_round, players, user_id)
        
        slots = max(2, len(players)) if is_pro else 2
        score_text = format_scoreboard(players, max_slots=slots)
        
        text = (
            f"Раунд {next_round}\n\n"
            f"Рахунок\n"
            f"{score_text}\n\n"
            f"Завдання: число {next_round}"
        )
        
        kb_text = "НОВА ГРА" if is_pro else "НОВА ГРА ДО 10"
        kb_data = "start_new_game" if is_pro else "start_free_10"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"undo_round_{current_round}")],
            [InlineKeyboardButton(text=kb_text, callback_data=kb_data)]
        ])
        
        await message.answer(text, reply_markup=kb)

# --- WEBHOOK FASTAPI СЕРВЕР ТА ЛІФСПАН МЕНЕДЖЕР ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Старт сервісу: реєстрація вебхука та ініціалізація бази даних
    await get_db_pool()
    await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    yield
    # Зупинка сервісу: видалення вебхука та закриття пулу підключень
    await bot.delete_webhook()
    if db_pool:
        await db_pool.close()

# Передаємо lifespan безпосередньо в ініціалізацію FastAPI додатка
app = FastAPI(lifespan=lifespan)

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)

@app.post("/monobank-webhook")
async def monobank_webhook(request: Request):
    data = await request.json()
    amount = data.get("data", {}).get("amount", 0)
    
    # Активація PRO при сумі від 100 грн (10000 копійок)
    if amount >= 10000:
        comment = data.get("data", {}).get("comment", "")
        user_id = None
        
        # Шукаємо Telegram ID у коментарі до транзакції
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
                    f"ОПЛАТА УСПІШНА!\n\n"
                    f"Дякую, оплата є!\n"
                    f"– {u_name} тепер Pro\n"
                    f"– відкрито 100 раундів\n"
                    f"– відкрито 10 гравців"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_new_game")]
                ])
                await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
            except TelegramAPIError:
                pass
                
    return Response(status_code=200)

@app.get("/")
async def root_health_check():
    return {"status": "healthy", "bot": "100 PHOTO"}


# Блок для локального запуску скрипта
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=True)
