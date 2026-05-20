import os
import json
import asyncio
from contextlib import asynccontextmanager
import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- КОНФІГУРАЦІЯ ТА ЗМІННІ ОТОЧЕННЯ ---
ADMIN_ID = 124303561
BASE_URL = os.getenv("BASE_URL", "https://stophotobot-1.onrender.com")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8115804787:AAF6IltbmJx9CkU-ROMEhVoFVgG8xVS579Q")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:stophotobot777@db.wfdgeuhdfluqccbunhiz.supabase.co:6543/postgres")
MONOBANK_JAR_URL = "https://send.monobank.ua/jar/YOUR_JAR_ID"  # Налаштовується за потреби

# Ініціалізація додатку Telegram-бота
ptb_app = Application.builder().token(BOT_TOKEN).build()

# --- LIFESPAN ДЛЯ РЕЄСТРАЦІЇ WEBHOOK ТА ПІДКЛЮЧЕННЯ БД ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Асинхронне підключення до пул-конекцій бази даних без примусових автозамін рядка
    app.state.db_pool = await asyncpg.create_pool(dsn=DATABASE_URL)
    
    # Створення необхідної структури таблиць, якщо вони відсутні
    async with app.state.db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_pro BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS games (
                chat_id BIGINT PRIMARY KEY REFERENCES chats(chat_id),
                current_round INT DEFAULT 0,
                is_pro BOOLEAN DEFAULT FALSE,
                scores JSONB DEFAULT '{}'::jsonb,
                history JSONB DEFAULT '[]'::jsonb,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS clicks (
                user_id BIGINT,
                chat_id BIGINT,
                clicked_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, chat_id)
            );
        """)
        
    # Ініціалізація та встановлення вебхука для бота
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(url=f"{BASE_URL}/webhook")
    app.state.bot = ptb_app.bot
    yield
    # Видалення вебхука та закриття пулу при зупинці додатку
    await ptb_app.bot.delete_webhook()
    await ptb_app.uninitialize()
    await app.state.db_pool.close()

app = FastAPI(lifespan=lifespan)

# --- ДОПОМІЖНІ ФУНКЦІЇ ЛОГІКИ ---
async def register_user_and_chat(user, chat, pool):
    if not user or not chat:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chats (chat_id) VALUES ($1) ON CONFLICT (chat_id) DO NOTHING",
            chat.id
        )
        full_name = user.full_name or ""
        await conn.execute(
            "INSERT INTO users (user_id, username, full_name) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, full_name = EXCLUDED.full_name",
            user.id, user.username, full_name
        )

async def get_game_pro_status(chat_id, user_id, pool):
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow("SELECT is_pro FROM users WHERE user_id = $1", user_id)
        user_is_pro = user_row['is_pro'] if user_row else False
        
        if user_is_pro:
            await conn.execute(
                "INSERT INTO games (chat_id, is_pro) VALUES ($1, TRUE) "
                "ON CONFLICT (chat_id) DO UPDATE SET is_pro = TRUE",
                chat_id
            )
            return True
            
        game_row = await conn.fetchrow("SELECT is_pro FROM games WHERE chat_id = $1", chat_id)
        return game_row['is_pro'] if game_row else False

async def enforce_limits(update: Update, context: ContextTypes.DEFAULT_TYPE, pool) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        return True
        
    await register_user_and_chat(user, chat, pool)
    is_pro = await get_game_pro_status(chat.id, user.id, pool)
    
    try:
        member_count = await context.bot.get_chat_member_count(chat.id) - 1
    except Exception:
        member_count = 2
        
    if not is_pro:
        if member_count == 1:
            text = "Щоб грати, додайте в групу другого гравця.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("НОВА ГРА ДО 10", callback_data="new_game_10")]])
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=kb)
            return False
        elif member_count >= 3:
            text = (
                "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n"
                "Pro-версія гри:\n"
                "- до 10 гравців\n"
                "- до 100 раундів назавжди\n"
                "- у всіх чатах Pro-гравця"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("КУПИТИ PRO-ВЕРСІЮ", url=f"{BASE_URL}/pay/{user.id}/{chat.id}")]])
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=kb)
            return False
    else:
        if member_count == 1:
            text = "Щоб грати, додайте в групу другого гравця.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("НОВА ГРА", callback_data="new_game_pro")]])
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=kb)
            return False
        elif member_count > 10:
            text = "На жаль, грати може максимум 10 гравців.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("НАС ВЖЕ 10", callback_data="check_10")]])
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=kb)
            return False
            
    return True

# --- ВІДОБРАЖЕННЯ ПОСТІВ ГРИ ---
async def send_rules_post(chat_id: int, bot, is_pro: bool):
    text = (
        'Вітаємо у грі <a href="https://t.me/stophotobot">100 PHOTO</a>!\n'
        'Правила гри:\n\n'
        '1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n'
        '2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 фото.\n'
        'За кожне фото гравець отримує 1 бал.\n\n'
        '3. Числа не можна створювати (викладати предметами) або писати самому.\n'
        'Лише фотографувати їх вдома, на вулиці тощо.\n\n'
        '4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо).\n'
        'Локації мають бути різними.\n\n'
        '5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\n'
        'Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n'
        'За бажанням, придумайте приз переможцю.\n\n'
        'Натхнення!'
    )
    if not is_pro:
        buttons = [
            [InlineKeyboardButton("НОВА ГРА ДО 10", callback_data="new_game_10")],
            [InlineKeyboardButton("НОВА ГРА ДО 100", callback_data="go_pay")],
            [InlineKeyboardButton("ДОДАТИ ГРАВЦІВ", callback_data="go_pay")]
        ]
    else:
        buttons = [[InlineKeyboardButton("НОВА ГРА", callback_data="new_game_pro")]]
        
    try:
        with open("1.png", "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    except Exception:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

async def send_task_post(chat_id: int, bot, round_num: int, scores: dict, is_pro: bool):
    if not scores:
        scores_text = "player 1: 0\nplayer 2: 0"
        if is_pro:
            scores_text += "\n…"
    else:
        scores_text = "\n".join([f"{u}: {s}" for u, s in scores.items()])
        
    text = f"Рахунок\n{scores_text}\n\nЗавдання: {round_num}"
    
    if round_num == 1:
        text += "\n\nЗнайди і сфотографуй число 1."
        try:
            with open("2.png", "rb") as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=text)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=text)
