import os
import logging
import asyncio
import asyncpg
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton

# Налаштування логів для Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

# Якщо в Render рядок підключення починається з postgresql://, міняємо на postgres:// для asyncpg
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgres://", 1)

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# Тимчасові дані для ігрового процесу в оперативці
GAMES_DATA = {} 
ADMIN_ID = 124303561 

# --- БЛОК РОБОТИ З БАЗОЮ ДАНИХ (SUPABASE) ---
async def init_db():
    """Створює таблиці в Supabase, якщо їх немає"""
    if not DATABASE_URL:
        logger.error("DATABASE_URL не знайдено в змінних оточення!")
        return
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''');
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''');
        logger.info("База даних успішно ініціалізована, таблиці створені!")
    except Exception as e:
        logger.error(f"Помилка ініціалізації БД: {e}")
    finally:
        await conn.close()

async def log_chat_to_db(chat_id: int):
    if not DATABASE_URL: return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('INSERT INTO chats (chat_id) VALUES ($1) ON CONFLICT (chat_id) DO NOTHING', chat_id)
        await conn.close()
    except Exception as e:
        logger.error(f"Помилка запису чату в БД: {e}")

async def log_user_to_db(user_id: int):
    if not DATABASE_URL: return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING', user_id)
        await conn.close()
    except Exception as e:
        logger.error(f"Помилка запису користувача в БД: {e}")

async def get_db_stats():
    if not DATABASE_URL: return 0, 0
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        chats_count = await conn.fetchval('SELECT COUNT(*) FROM chats')
        users_count = await conn.fetchval('SELECT COUNT(*) FROM users')
        await conn.close()
        return chats_count, users_count
    except Exception as e:
        logger.error(f"Помилка отримання статистики з БД: {e}")
        return 0, 0

def get_user_name(user: types.User) -> str:
    if user and user.first_name:
        return user.first_name
    return f"@{user.username}" if user and user.username else f"ID: {user.id if user else 'Unknown'}"

# --- ТЕКСТ ПРАВИЛ ТА КНОПКИ ---
RULES_TEXT = (
    "Вітаємо у грі <a href='https://t.me/100photobot'>100 PHOTO</a>!\n\n"
    "Правила гри:\n\n"
    "1. Завдання гравців – фотографувати числа (1, 2, 3) i надсилати у цей чат.\n\n"
    "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 photo. За кожне photo гравець отримує 1 бал.\n\n"
    "3. Числа не можна створювати (викладати предметами) або писати самому. Лише фотографувати їх вдома, на вулиці тощо.\n\n"
    "4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, номери паркомісць тощо). Локації мають бути різними.\n\n"
    "5. Якщо надіслане photo не відповідає правилам, це photo можна відмінити і почати раунд заново.\n\n"
    "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
    "За бажанням, придумайте приз переможцю.\n\n"
    "Натхнення!"
)

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 10 ]", callback_data="start_free")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 100 ]", callback_data="trigger_pay")],
        [InlineKeyboardButton(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data="trigger_pay")]
    ])

# --- ЛОГІКА ТЕЛЕГРАМ БОТА ---

@dp.message(F.text.in_({"/stat", "/stats", "/admin_stats", "Стат"}))
async def show_admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    db_chats, db_users = await get_db_stats()
    active_chats = len(GAMES_DATA)
    active_users = sum(len(game["scores"]) for game in GAMES_DATA.values())
    stats_text = (
        "📊 **АКТУАЛЬНА СТАТИСТИКА БОТА (Supabase)**\n\n"
        "🗄️ **Збережено в базі даних назавжди:**\n"
        f"├ Всього підключено чатів: {db_chats}\n"
        f"└ Всього унікальних людей: {db_users}\n\n"
        "🚀 **Прямо зараз в реальному часі:**\n"
        f"├ Активних сесій (ігор): {active_chats}\n"
        f"└ Грає людей в цих чатах: {active_users}"
    )
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(F.new_chat_members)
async def on_bot_join(message: types.Message):
    bot_added = any(member.id == bot.id for member in message.new_chat_members)
    if bot_added:
        await log_chat_to_db(message.chat.id)
        await log_user_to_db(message.from_user.id)
        await message.answer(RULES_TEXT, parse_mode="HTML", reply_markup=get_main_keyboard(), disable_web_page_preview=True)

@dp.message(F.text.in_({"/start", "/game", "/play", "Старт", "game", "play", "Start"}))
async def on_command_start(message: types.Message):
    await log_chat_to_db(message.chat.id)
    await log_user_to_db(message.from_user.id)
    await message.answer(RULES_TEXT, parse_mode="HTML", reply_markup=get_main_keyboard(), disable_web_page_preview=True)

# Окремий точковий обробник для скасування раунду
@dp.callback_query(F.data == "cancel_last")
async def cancel_last_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in GAMES_DATA or not GAMES_DATA[chat_id]["history"]:
        await callback.answer("Немає раундів для скасування!", show_alert=True)
        return
        
    game = GAMES_DATA[chat_id]
    last_user, last_round = game["history"].pop()
    if last_user in game["scores"] and game["scores"][last_user] > 0:
        game["scores"][last_user] -= 1
        
    game["round"] = last_round
    
    # Формуємо рахунок для відображення при скасуванні
    if game["scores"]:
        scores_text = "\n".join([f"{u}: {s}" for u, s in game["scores"].items()])
        if len(game["scores"]) == 1:
            scores_text += "\nГравець 2: 0"
    else:
        creator_name = game.get("creator", "Гравець 1")
        scores_text = f"{creator_name}: 0\nГравець 2: 0"
        
    task_text = f"Завдання: {last_round}\n\n{scores_text}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"[ ОБНУЛИТИ РАУНД {last_round-1} ]", callback_data="cancel_last")] if last_round > 1 else [],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 10 ]", callback_data="start_free")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 100 ]", callback_data="trigger_pay")],
        [InlineKeyboardButton(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data="trigger_pay")]
    ])
    kb.inline_keyboard = [row for row in kb.inline_keyboard if row]
    
    await callback.message.answer(task_text, reply_markup=kb)
    await callback.answer("Останній раунд скасовано!")

# Загальний обробник для інших кнопок меню
@dp.callback_query()
async def process_callbacks(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await log_user_to_db(callback.from_user.id)
    
    if callback.data == "start_free":
        chat_member_count = await bot.get_chat_member_count(chat_id)
        if chat_member_count > 3:
            await show_payment_post(chat_id)
            await callback.answer()
            return
            
        await log_chat_to_db(chat_id)
        
        # Отримуємо реальне ім'я того, хто натиснув кнопку "Нова гра"
        creator_name = get_user_name(callback.from_user)
        
        GAMES_DATA[chat_id] = {
            "status": "free",
            "round": 1,
            "creator": creator_name,
            "scores": {},
            "history": []
        }
        
        # Виводимо гарний стартовий шаблон із реальним ім'ям та замінником для другого гравця
        text = (
            "Завдання: 1\n\n"
            f"{creator_name}: 0\n"
            "Гравець 2: 0\n\n"
            "Знайди і сфотографуй число 1."
        )
        await callback.message.answer(text)
        await callback.answer()

    elif callback.data == "trigger_pay":
        await show_payment_post(chat_id)
        await callback.answer()
        
    elif callback.data == "buy_pro":
        await callback.message.answer("Обробка запиту... Платіжна система налаштовується еквайрингом.")
        await callback.answer()

async def show_payment_post(chat_id: int):
    text = (
        "Pro-версія гри:\n"
        "- безлімітна к-сть гравців\n"
        "- гра до 100 раундів назавжди\n"
        "- у всіх чатах Pro-гравця"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[ КУПИТИ PRO-ВЕРСІЮ ]", callback_data="buy_pro")],
        [InlineKeyboardButton(text="[ ПРОДОВЖИТИ ГРУ УДВОХ ]", callback_data="start_free")]
    ])
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)

@dp.message(F.photo)
async def handle_game_photo(message: types.Message):
    chat_id = message.chat.id
    if chat_id not in GAMES_DATA:
        return
        
    game = GAMES_DATA[chat_id]
    user_name = get_user_name(message.from_user)
    await log_user_to_db(message.from_user.id)
    
    chat_member_count = await bot.get_chat_member_count(chat_id)
    if game["status"] == "free" and chat_member_count > 3:
        await show_payment_post(chat_id)
        return

    current_round = game["round"]
    max_rounds = 10
    
    game["scores"][user_name] = game["scores"].get(user_name, 0) + 1
    game["history"].append((user_name, current_round))
    
    if current_round >= max_rounds:
        winner = max(game["scores"], key=game["scores"].get)
        scores_text = "\n".join([f"{u}: {s}" for u, s in game["scores"].items()])
        fin_text = f"Переможець: {winner}\n\nРахунок:\n{scores_text}\n\nНе забудь про свій приз!"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="[ ОБНУЛИТИ РАУНД 10 ]", callback_data="cancel_last")],
            [InlineKeyboardButton(text="[ НОВА ГРА ДО 10 ]", callback_data="start_free")],
            [InlineKeyboardButton(text="[ НОВА ГРА ДО 100 ]", callback_data="trigger_pay")],
            [InlineKeyboardButton(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data="trigger_pay")]
        ])
        await message.answer(fin_text, reply_markup=kb)
        GAMES_DATA.pop(chat_id, None)
        return

    game["round"] += 1
    next_round = game["round"]
    
    # Формуємо живий список результатів
    scores_text = "\n".join([f"{u}: {s}" for u, s in game["scores"].items()])
    
    # Якщо досі активний лише один гравець, підставляємо "Гравець 2: 0"
    if len(game["scores"]) == 1:
        creator = game.get("creator", "Гравець 1")
        if creator not in game["scores"]:
            scores_text = f"{creator}: 0\n" + scores_text
        else:
            scores_text += "\nГравець 2: 0"

    task_text = f"Завдання: {next_round}\n\n{scores_text}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"[ ОБНУЛИТИ РАУНД {next_round-1} ]", callback_data="cancel_last")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 10 ]", callback_data="start_free")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 100 ]", callback_data="trigger_pay")],
        [InlineKeyboardButton(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data="trigger_pay")]
    ])
    await message.answer(task_text, reply_markup=kb)

@dp.message()
async def ignore_text_messages(message: types.Message):
    pass

# --- НАЛАШТУВАННЯ ВЕБХУКІВ ДЛЯ FASTAPI ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        json_str = await request.json()
        update = Update(**json_str)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Помилка всередині вебхука: {e}")
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "working", "info": "100 PHOTO Bot"}

@app.on_event("startup")
async def on_startup():
    await init_db()
    if BASE_URL:
        logger.info("Оновлення вебхука в Telegram...")
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(f"{BASE_URL}/webhook")
            logger.info("Вебхук успішно перевстановлено!")
        except Exception as e:
            logger.error(f"Помилка встановлення вебхука: {e}")
