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

# Зчитування змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Критична помилка: Відсутні обов'язкові змінні оточення BOT_TOKEN, BASE_URL або DATABASE_URL!")

# Налаштування ендпоінту для Вебхука
WEBHOOK_PATH = f"/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# Ініціалізація бота та диспетчера з підтримкою HTML
bot = Bot(
    token=BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

DB_POOL = None

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
        # Створення таблиці ігор з правильними типами
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS games (
                chat_id BIGINT PRIMARY KEY,
                status TEXT DEFAULT 'registration',
                round_number INT DEFAULT 0,
                players JSONB DEFAULT '{}'::jsonb,
                current_word_data JSONB
            );
        ''')
        # Створення таблиці PRO користувачів
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pro_users (
                user_id BIGINT PRIMARY KEY,
                is_pro BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        logger.info("Таблиці та структури колонок в БД перевірено/оновлено.")

async def load_game(chat_id: int):
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, round_number, players, current_word_data FROM games WHERE chat_id = $1", chat_id)
        if row:
            return {
                "status": row["status"],
                "round_number": row["round_number"],
                "players": json.loads(row["players"]) if isinstance(row["players"], str) else row["players"],
                "current_word_data": json.loads(row["current_word_data"]) if row["current_word_data"] else None
            }
        return None

async def save_game(chat_id: int, status: str, round_number: int, players: dict, current_word_data: dict = None):
    pool = await get_db_connection()
    players_json = json.dumps(players)
    current_word_json = json.dumps(current_word_data) if current_word_data else None
    
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO games (chat_id, status, round_number, players, current_word_data)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) 
            DO UPDATE SET status = $2, round_number = $3, players = $4, current_word_data = $5
        ''', chat_id, status, round_number, players_json, current_word_json)

async def is_user_pro(user_id: int) -> bool:
    if user_id in [124303561]:  # Твій розробницький ID завжди PRO
        return True
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT is_pro FROM pro_users WHERE user_id = $1", user_id)
        return bool(val)

async def set_user_pro_status(user_id: int, status: bool):
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO pro_users (user_id, is_pro, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET is_pro = $2, updated_at = CURRENT_TIMESTAMP
        ''', user_id, status)

# ==========================================
# ВАРД-ФІЛЬТРИ ТА ПЕРЕВІРКА КІЛЬКОСТІ ЛУДЕЙ
# ==========================================

async def get_chat_players_count(chat_id: int) -> int:
    try:
        count = await bot.get_chat_member_count(chat_id)
        return count
    except Exception as e:
        logger.error(f"Помилка отримання кількості учасників: {e}")
        return 0

# ==========================================
# ЛОГІКА ХЕНДЛЕРІВ TELEGRAM (aiogram)
# ==========================================

@dp.message(Command("stat"))
async def admin_stat(message: types.Message):
    if message.chat.type == "private" and message.from_user.id == 124303561:
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            total_games = await conn.fetchval("SELECT COUNT(*) FROM games")
            total_pro = await conn.fetchval("SELECT COUNT(*) FROM pro_users WHERE is_pro = true")
            
        text = (
            f" Bars <b>Статистика Бота:</b>\n\n"
            f"• Всього ігор у групах: {total_games}\n"
            f"• Всього PRO користувачів: {total_pro}"
        )
        await message.answer(text)

@dp.message(F.chat.type == "private")
async def private_stub(message: types.Message):
    if message.from_user.id == 124303561 and message.text.startswith("/stat"):
        return
        
    text = (
        "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу).\n\n"
        "Знайдеш мене через пошук – @stophotobot"
    )
    await message.answer(text)

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added_to_group(event: types.ChatMemberUpdated):
    chat_id = event.chat.id
    logger.info(f"Бота додано в групу: {chat_id}")
    
    await save_game(chat_id, "registration", 0, {})
    
    # Перевірка кількості людей у групі
    count = await get_chat_players_count(chat_id)
    # Віднімаємо бота (count - 1), якщо рахувати тільки людей
    actual_humans = count - 1 if count > 0 else 1

    if actual_humans < 2:
        text = (
            "<b>Щоб грати, додайте в групу другого гравця.</b>\n\n"
            "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
            "📸 <b>ПРАВИЛА ГРИ «100 PHOTO»</b>\n"
            "Люди фотографують цифри у реальному світі та надсилають фото в чат."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
        ])
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        return

    if actual_humans > 10:
        text = (
            "<b>На жаль, грати може максимум 10 гравців.</b>\n\n"
            "У вашій групі забагато учасників для цього бота. "
            "Щоб перезапустити бота, напишіть в чат команду /start або /play."
        )
        await bot.send_message(chat_id=chat_id, text=text)
        return

    text = (
        "📸 <b>ПРАВИЛА ГРИ «100 PHOTO»</b>\n\n"
        "Люди фотографують цифри у реальному світі та надсилають фото в чат.\n"
        "Бот рахує бали та веде гру по раундах.\n\n"
        "Щоб розпочати, тисніть кнопку нижче!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
        [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="start_pro_buy")]
    ])
    
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)

@dp.message(Command("start", "play"))
async def manual_start_in_group(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        chat_id = message.chat.id
        await save_game(chat_id, "registration", 0, {})
        
        count = await get_chat_players_count(chat_id)
        actual_humans = count - 1 if count > 0 else 1

        if actual_humans < 2:
            text = (
                "<b>Щоб грати, додайте в групу другого гравця.</b>\n\n"
                "Щоб перезапустити бота, напишіть в чат команду /start або /play."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
            ])
            await message.answer(text, reply_markup=kb)
            return

        text = (
            "<b>Гра перезапущена!</b> 📸\n\n"
            "Оберіть режим гри нижче:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="start_pro_buy")]
        ])
        await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "start_free_10")
async def start_free_game(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    
    players = {}
    current_word_data = {"number": 1}
    await save_game(chat_id, "playing_free", 1, players, current_word_data)
    
    text = (
        "<b>Раунд 1.</b>\n\n"
        "Рахунок:\n<i>Поки немає гравців</i>\n\n"
        "<b>Завдання:</b> сфотографуй число 1️⃣!"
    )
    
    await callback.message.edit_text(text=text, reply_markup=None)
    await callback.answer()

@dp.callback_query(F.data.startswith("start_pro_game_active"))
async def start_pro_game_active(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    players = {}
    current_word_data = {"number": 1}
    await save_game(chat_id, "playing_pro", 1, players, current_word_data)
    
    text = (
        "<b>Раунд 1 (PRO Режим).</b>\n\n"
        "Рахунок:\n<i>Поки немає гравців</i>\n\n"
        "<b>Завдання:</b> сфотографуй число 1️⃣!"
    )
    await callback.message.edit_text(text=text, reply_markup=None)
    await callback.answer()

@dp.callback_query(F.data == "start_pro_buy")
async def show_pro_payment(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    # Перевіряємо, чи користувач вже купував PRO раніше
    if await is_user_pro(user_id):
        chat_id = callback.message.chat.id
        players = {}
        current_word_data = {"number": 1}
        await save_game(chat_id, "playing_pro", 1, players, current_word_data)
        
        text = (
            "<b>У вас вже є PRO статус!</b> 🎉\n"
            "Запускаємо гру до 100 раундів.\n\n"
            "<b>Раунд 1.</b>\n"
            "<b>Завдання:</b> сфотографуй число 1️⃣!"
        )
        await callback.message.edit_text(text=text, reply_markup=None)
        await callback.answer()
        return

    mono_link = f"https://send.monobank.ua/jar/8Sg7bYg9Xb?a=100&m={user_id}"
    
    text = (
        "<b>Pro-версія гри:</b>\n"
        "• до 10 гравців у чаті\n"
        "• до 100 раундів назавжди\n"
        "• працює у всіх чатах Pro-гравця\n\n"
        "Для активації оплатіть 100 грн через Monobank Банку. "
        "Активація відбудеться автоматично протягом хвилини після оплати!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ОПЛАТИТИ 100 ГРН", url=mono_link)]
    ])
    
    await callback.message.reply(text=text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("clear_round_"))
async def clear_round_handler(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = await load_game(chat_id)
    
    if not game or game["status"] not in ["playing_free", "playing_pro"]:
        await callback.answer("Гра не активна.")
        return
        
    try:
        target_round = int(callback.data.replace("clear_round_", ""))
    except ValueError:
        await callback.answer("Помилка даних.")
        return

    # Зменшуємо раунд назад
    players = game["players"]
    current_word_data = {"number": target_round}
    await save_game(chat_id, game["status"], target_round, players, current_word_data)
    
    scoreboard = "\n".join([f"{p['name']}: {p['score']}" for p in players.values()]) if players else "Поки немає гравців"
    text = (
        f"🔄 <b>Раунд {target_round} обнулено та запущено заново!</b>\n\n"
        f"<b>Рахунок:</b>\n{scoreboard}\n\n"
        f"<b>Завдання:</b> сфотографуй число {target_round}️⃣!"
    )
    
    # Оновлюємо кнопки під раундом
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 ОБНУЛИТИ РАУНД", callback_data=f"clear_round_{target_round}")],
        [InlineKeyboardButton(text="🆕 НОВА ГРА", callback_data="start_free_10" if game["status"] == "playing_free" else "start_pro_game_active")]
    ])
    
    await callback.message.answer(text=text, reply_markup=kb)
    await callback.answer("Раунд успішно скинуто!")

@dp.message(F.chat.type.in_(["group", "supergroup"]) & F.photo)
async def handle_game_photo(message: types.Message):
    chat_id = message.chat.id
    game = await load_game(chat_id)
    
    if not game or game["status"] not in ["playing_free", "playing_pro"]:
        return

    round_num = game["round_number"]
    players = game["players"]
    user_id = str(message.from_user.id)
    u_name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name

    # Перевірка лімітів FREE версії на 3+ людей в чаті
    if game["status"] == "playing_free":
        if user_id not in players and len(players) >= 2:
            # Можливо, той хто надсилає фото — PRO користувач?
            if not await is_user_pro(message.from_user.id):
                text = (
                    "<b>Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.</b>\n\n"
                    "Pro-версія гри:\n"
                    "- до 10 гравців\n"
                    "- до 100 раундів назавжди\n\n"
                    "Тисніть кнопку нижче для покупки!"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="start_pro_buy")]
                ])
                await message.reply(text, reply_markup=kb)
                return

    # Зараховуємо бал
    if user_id not in players:
        players[user_id] = {"name": u_name, "score": 0}
        
    players[user_id]["score"] += 1
    
    # Перевірка фіналу гри
    max_rounds = 10 if game["status"] == "playing_free" else 100
    if round_num >= max_rounds:
        winner = max(players.values(), key=lambda x: x["score"])
        scoreboard = "\n".join([f"{p['name']}: {p['score']} балів" for p in players.values()])
        text = (
            f"🎉 <b>КІНЕЦЬ ГРИ!</b> 🎉\n\n"
            f"🏆 Переможець: <b>{winner['name']}</b>\n\n"
            f"<b>Фінальний рахунок:</b>\n{scoreboard}\n\n"
            f"Дякуємо за гру! Тисніть /start для нового матчу."
        )
        await save_game(chat_id, "finished", 0, {})
        await message.answer(text)
        return

    # Перехід на наступний раунд
    next_round = round_num + 1
    current_word_data = {"number": next_round}
    await save_game(chat_id, game["status"], next_round, players, current_word_data)

    scoreboard = "\n".join([f"{p['name']}: {p['score']}" for p in players.values()])
    text = (
        f"<b>Раунд {next_round}.</b>\n\n"
        f"<b>Рахунок:</b>\n{scoreboard}\n\n"
        f"<b>Завдання:</b> сфотографуй число {next_round}️⃣!"
    )
    
    # Динамічні кнопки керування під кожним раундом
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔄 ОБНУЛИТИ РАУНД {next_round - 1}", callback_data=f"clear_round_{next_round - 1}")],
        [InlineKeyboardButton(text="🆕 НОВА ГРА", callback_data="start_free_10" if game["status"] == "playing_free" else "start_pro_game_active")]
    ])
    
    await message.answer(text, reply_markup=kb)

# ==========================================
# МОНІТОРИНГ ОПЛАТ (Monobank Webhook) та FastAPI
# ==========================================

app = FastAPI()

@app.post("/webhook")
async def bot_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
    
    update = types.Update(**data)
    await dp.feed_update(bot, update)
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
        
        # Перевірка суми (100 грн = 10000 копійок)
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
                        f"<b>ОПЛАТА УСПІШНА!</b> 🎉\n\n"
                        f"Дякую за підтримку!\n"
                        f"– {u_name} тепер має статус <b>Pro</b> назавжди!\n"
                        f"– Відкрито ліміт до 100 раундів\n"
                        f"– Дозволено грати до 10 учасників у групах"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="start_pro_game_active")]
                    ])
                    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати повідомлення про оплату в приват: {e}")
                    
    return Response(status_code=200)

@app.get("/")
async def root():
    return {"status": "working", "bot": "100_photo_bot"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Код при старті додатка:
    await init_db()
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    logger.info("Вебхук успішно встановлено!")
    
    yield
    
    # Код при вимкненні сервера:
    logger.info("Закриття додатка, очищення ресурсів...")
    await dp.storage.close()
    
    # Правильне закриття сесії в aiogram 3:
    if bot.session and not bot.session.closed:
        await bot.session.close()
        
    if DB_POOL:
        await DB_POOL.close()
        logger.info("Пул підключень до БД успішно закрито.")

app.router.lifespan_context = lifespan
