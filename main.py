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
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:stophotobot777@db.wfdgeuhdfluqccbunhiz.supabase.co:6543/postgres")
MONOBANK_JAR_URL = "https://send.monobank.ua/jar/YOUR_JAR_ID"  # Налаштовується за потреби

# Абсолютна безпека: токен береться виключно із налаштувань Render
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Критична помилка: змінна оточення BOT_TOKEN не заповнена на Render!")

# Ініціалізація додатку Telegram-бота
ptb_app = Application.builder().token(BOT_TOKEN).build()

# --- LIFESPAN ДЛЯ РЕЄСТРАЦІЇ WEBHOOK ТА ПІДКЛЮЧЕННЯ БД ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Асинхронне підключення до пул-конекцій бази даних
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
        '5. Якщо надіслане photo не відповідає правилам, це фото можна відмінити і почати раунд заново.\n'
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
    else:
        if not is_pro:
            buttons = [
                [InlineKeyboardButton(f"ОБНУЛИТИ РАУНД {round_num-1}", callback_data=f"undo_{round_num-1}")],
                [InlineKeyboardButton("НОВА ГРА ДО 10", callback_data="new_game_10")],
                [InlineKeyboardButton("НОВА ГРА ДО 100", callback_data="go_pay")],
                [InlineKeyboardButton("ДОДАТИ ГРАВЦІВ", callback_data="go_pay")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton(f"ОБНУЛИТИ РАУНД {round_num-1}", callback_data=f"undo_{round_num-1}")],
                [InlineKeyboardButton("НОВА ГРА", callback_data="new_game_pro")]
            ]
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(buttons))

async def send_end_game_post(chat_id: int, bot, scores: dict, is_pro: bool):
    scores_text = "\n".join([f"{u}: {s}" for u, s in scores.items()]) if scores else "player 1: 0\nplayer 2: 0"
    winner = max(scores, key=scores.get) if scores else "@user"
    
    text = f"Рахунок\n{scores_text}\n\nПереможець: {winner}\n\nНе забудь про свій приз!"
    
    if not is_pro:
        buttons = [
            [InlineKeyboardButton("ОБНУЛИТИ РАУНД 10", callback_data="undo_10")],
            [InlineKeyboardButton("НОВА ГРА ДО 10", callback_data="new_game_10")],
            [InlineKeyboardButton("НОВА ГРА ДО 100", callback_data="go_pay")],
            [InlineKeyboardButton("ДОДАТИ ГРАВЦІВ", callback_data="go_pay")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton("ОБНУЛИТИ РАУНД 100", callback_data="undo_100")],
            [InlineKeyboardButton("НОВА ГРА", callback_data="new_game_pro")]
        ]
        
    try:
        with open("3.png", "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(buttons))

# --- ОБРОБНИКИ ТЕЛЕГРАМ-ПОДІЙ ---
async def start_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        return await private_chat_handler(update, context)
        
    pool = app.state.db_pool
    await register_user_and_chat(user, chat, pool)
    is_pro = await get_game_pro_status(chat.id, user.id, pool)
    
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO games (chat_id, current_round, is_pro, scores, history) "
            "VALUES ($1, 0, $2, '{}'::jsonb, '[]'::jsonb) "
            "ON CONFLICT (chat_id) DO UPDATE SET current_round = 0, is_pro = EXCLUDED.is_pro, scores = '{}'::jsonb, history = '[]'::jsonb",
            chat.id, is_pro
        )
        
    if not await enforce_limits(update, context, pool):
        return
        
    await send_rules_post(chat.id, context.bot, is_pro)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    user = query.from_user
    if not chat or chat.type == "private":
        return
        
    pool = app.state.db_pool
    data = query.data
    
    if data == "go_pay":
        text = "Pro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах Pro-гравця"
        buttons = [
            [InlineKeyboardButton("КУПИТИ PRO-ВЕРСІЮ", url=f"{BASE_URL}/pay/{user.id}/{chat.id}")],
            [InlineKeyboardButton("ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="new_game_10")]
        ]
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return
        
    if data in ["new_game_10", "new_game_pro", "check_10"]:
        is_pro = (data == "new_game_pro")
        async with pool.acquire() as conn:
            if is_pro:
                await conn.execute("UPDATE games SET is_pro = TRUE WHERE chat_id = $1", chat.id)
            else:
                is_pro = await get_game_pro_status(chat.id, user.id, pool)
            
            if data != "check_10":
                await conn.execute("UPDATE games SET current_round = 1, scores = '{}'::jsonb, history = '[]'::jsonb WHERE chat_id = $1", chat.id)
                
        update.message = query.message
        if not await enforce_limits(update, context, pool):
            return
            
        if data != "check_10":
            await send_task_post(chat.id, context.bot, 1, {}, is_pro)
        return
        
    if data.startswith("undo_"):
        target_round = int(data.split("_")[1])
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT is_pro, history FROM games WHERE chat_id = $1", chat.id)
            if not row:
                return
            is_pro = row['is_pro']
            history = json.loads(row['history'])
            
            state_to_restore = next((h for h in reversed(history) if h.get("round") == target_round), None)
            if state_to_restore:
                await conn.execute(
                    "UPDATE games SET current_round = $1, scores = $2 WHERE chat_id = $3",
                    state_to_restore["round"], json.dumps(state_to_restore["scores"]), chat.id
                )
                await send_task_post(chat.id, context.bot, state_to_restore["round"], state_to_restore["scores"], is_pro)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private" or not update.message.photo:
        return
        
    pool = app.state.db_pool
    if not await enforce_limits(update, context, pool):
        return
        
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT current_round, is_pro, scores, history FROM games WHERE chat_id = $1", chat.id)
        if not row or row['current_round'] == 0:
            return
            
        current_round = row['current_round']
        is_pro = row['is_pro']
        scores = json.loads(row['scores'])
        history = json.loads(row['history'])
        
        history.append({"round": current_round, "scores": dict(scores)})
        user_display = user.full_name or (f"@{user.username}" if user.username else user.first_name)
        scores[user_display] = scores.get(user_display, 0) + 1
        
        max_rounds = 100 if is_pro else 10
        if current_round >= max_rounds:
            await conn.execute("UPDATE games SET current_round = 0, scores = $1, history = $2 WHERE chat_id = $3", json.dumps(scores), json.dumps(history), chat.id)
            await send_end_game_post(chat.id, context.bot, scores, is_pro)
        else:
            next_round = current_round + 1
            await conn.execute("UPDATE games SET current_round = $1, scores = $2, history = $3 WHERE chat_id = $4", next_round, json.dumps(scores), json.dumps(history), chat.id)
            await send_task_post(chat.id, context.bot, next_round, scores, is_pro)

async def private_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return
        
    if update.message:
        await update.message.reply_text(
            "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). Знайдеш мене через пошук @stophotobot"
        )

async def admin_stat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    pool = app.state.db_pool
    
    async with pool.acquire() as conn:
        tasks = [
            conn.fetchval("SELECT COUNT(*) FROM chats"),
            conn.fetchval("SELECT COUNT(*) FROM users"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = FALSE"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE"),
            
            conn.fetchval("SELECT COUNT(*) FROM chats WHERE created_at >= NOW() - INTERVAL '1 year'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '1 year'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= NOW() - INTERVAL '1 year'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= NOW() - INTERVAL '1 year'"),
            
            conn.fetchval("SELECT COUNT(*) FROM chats WHERE created_at >= NOW() - INTERVAL '30 days'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '30 days'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= NOW() - INTERVAL '30 days'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= NOW() - INTERVAL '30 days'"),
            
            conn.fetchval("SELECT COUNT(*) FROM chats WHERE created_at >= NOW() - INTERVAL '7 days'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= NOW() - INTERVAL '7 days'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= NOW() - INTERVAL '7 days'"),
            
            conn.fetchval("SELECT COUNT(*) FROM chats WHERE created_at >= NOW() - INTERVAL '24 hours'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '24 hours'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= NOW() - INTERVAL '24 hours'"),
            conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= NOW() - INTERVAL '24 hours'"),
        ]
        res = await asyncio.gather(*tasks)
        
    stat_text = (
        f"ЗА ВЕСЬ ЧАС:\n"
        f"- всі чати: {res[0]}\n"
        f"- всі юзери: {res[1]}\n"
        f"- free-юзери: {res[2]}\n"
        f"- pro-юзери: {res[3]}\n\n"
        f"ПРИРІСТ ЗА РІК:\n"
        f"- всі чати: +{res[4]}\n"
        f"- всі юзери: +{res[5]}\n"
        f"- free-юзери: +{res[6]}\n"
        f"- pro-юзери: +{res[7]}\n\n"
        f"ПРИРІСТ ЗА 30 ДНІВ:\n"
        f"- всі чати: +{res[8]}\n"
        f"- всі юзери: +{res[9]}\n"
        f"- free-юзери: +{res[10]}\n"
        f"- pro-юзери: +{res[11]}\n\n"
        f"ПРИРІСТ ЗА 7 ДНІВ:\n"
        f"- всі чати: +{res[12]}\n"
        f"- всі юзери: +{res[13]}\n"
        f"- free-юзери: +{res[14]}\n"
        f"- pro-юзери: +{res[15]}\n\n"
        f"ПРИРІСТ ЗА 24 ГОД:\n"
        f"- всі чати: +{res[16]}\n"
        f"- всі юзери: +{res[17]}\n"
        f"- free-юзери: +{res[18]}\n"
        f"- pro-юзери: +{res[19]}"
    )
    if update.message:
        await update.message.reply_text(stat_text)

# Реєстрація обробників у додатку бота
ptb_app.add_handler(CommandHandler(["start", "play"], start_game_command))
ptb_app.add_handler(CommandHandler("stat", admin_stat_handler))
ptb_app.add_handler(CallbackQueryHandler(callback_handler))
ptb_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
ptb_app.add_handler(MessageHandler(filters.ChatType.PRIVATE, private_chat_handler))

# --- ЕНДПОІНТИ FASTAPI ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)

@app.get("/pay/{user_id}/{chat_id}")
async def pay_redirect(user_id: int, chat_id: int):
    pool = app.state.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO clicks (user_id, chat_id) VALUES ($1, $2) "
            "ON CONFLICT (user_id, chat_id) DO UPDATE SET clicked_at = NOW()",
            user_id, chat_id
        )
    return RedirectResponse(url=f"{MONOBANK_JAR_URL}?a=100&t={user_id}")

@app.post("/monobank-webhook")
async def monobank_webhook(request: Request):
    data = await request.json()
    statement = data.get("data", {}).get("statementItem", {})
    amount = statement.get("amount", 0) or data.get("amount", 0)
    comment = statement.get("comment", "") or data.get("extRef", "")
    
    try:
        user_id = int(comment.strip())
    except ValueError:
        user_id = None
        
    if user_id and amount >= 10000:
        pool = app.state.db_pool
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, is_pro) VALUES ($1, TRUE) "
                "ON CONFLICT (user_id) DO UPDATE SET is_pro = TRUE",
                user_id
            )
            row = await conn.fetchrow(
                "SELECT chat_id FROM clicks WHERE user_id = $1 ORDER BY clicked_at DESC LIMIT 1",
                user_id
            )
            if row:
                chat_id = row['chat_id']
                await conn.execute("UPDATE games SET is_pro = TRUE WHERE chat_id = $1", chat_id)
                
                user_mention = f"User {user_id}"
                try:
                    member = await app.state.bot.get_chat_member(chat_id, user_id)
                    u = member.user
                    user_mention = u.full_name or (f"@{u.username}" if u.username else u.first_name)
                except Exception:
                    pass
                    
                text = f"Дякую, оплата є!\n– {user_mention} тепер Pro\n– відкрито 100 раундів\n– відкрито 10 гравців"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("НОВА ГРА", callback_data="new_game_pro")]])
                await app.state.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
                
    return {"status": "ok"}
