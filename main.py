import os
import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Update
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg

# Налаштування логування для моніторингу в панелі Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Зчитування змінних оточення (Суворо за ТЗ) — БЕЗПЕЧНО, БЕЗ ХАРДКОДУ
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
BASE_URL = os.getenv("BASE_URL") or "https://stophotobot-1.onrender.com"
DATABASE_URL = os.getenv("DATABASE_URL")
RULES_PHOTO_URL = os.getenv("RULES_PHOTO_URL") # Передається через Render за бажанням

if not TOKEN:
    raise ValueError("BOT_TOKEN або TELEGRAM_TOKEN не зафіксовано в системних змінних оточення!")

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None

# --- ШАБЛОНИ ТЕКСТІВ ТА КНОПОК СУВОРO ЗА ФАЙЛОМ ТЗ ---

def get_welcome_text() -> str:
    return (
        "Вітаємо у грі <a href=\"https://t.me/stophotobot\">100 PHOTO</a>!\n"
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n\n"
        "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 фото. "
        "За кожне photo гравець отримує 1 бал.\n\n"
        "3. Числа не можна створювати (викладати предметами) або писати самому. "
        "Лише фотографувати їх вдома, на вулиці тощо.\n\n"
        "4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо). "
        "Локації мають бути різними.\n\n"
        "5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\n\n"
        "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
        "За бажанням, придумайте приз переможцю.\n\n"
        "Натхнення!"
    )

def get_free_rules_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопки для поста ПРАВИЛА (FREE-ВЕРСІЯ)"""
    builder = InlineKeyboardBuilder()
    builder.button(text="НОВА ГРА ДО 10", callback_data="start_game_10")
    builder.button(text="НОВА ГРА ДО 100", callback_data="buy_pro")
    builder.button(text="ДОДАТИ ГРАВЦІВ", callback_data="add_players")
    builder.adjust(1)
    return builder.as_markup()

def get_free_game_keyboard(current_round: int) -> types.InlineKeyboardMarkup:
    """Кнопки для ПОСТУ ЗАВДАННЯ 2-10 (FREE-ВЕРСІЯ)"""
    builder = InlineKeyboardBuilder()
    # Кнопка обнулення показує попередній раунд (X-1)
    builder.button(text=f"ОБНУЛИТИ РАУНД {current_round - 1}", callback_data=f"cancel_round_{current_round - 1}")
    builder.button(text="НОВА ГРА ДО 10", callback_data="start_game_10")
    builder.button(text="НОВА ГРА ДО 100", callback_data="buy_pro")
    builder.button(text="ДОДАТИ ГРАВЦІВ", callback_data="add_players")
    builder.adjust(1)
    return builder.as_markup()

async def get_chat_players_tags(chat_id: int) -> list:
    try:
        admins = await bot.get_chat_administrators(chat_id)
        players = []
        for admin in admins:
            if not admin.user.is_bot:
                name = f"@{admin.user.username}" if admin.user.username else admin.user.first_name
                players.append(name)
        if len(players) >= 2:
            return players[:2]
        elif len(players) == 1:
            return [players[0], "player 2"]
    except Exception as e:
        logger.warning(f"Не вдалося отримати учасників чату: {e}")
    return ["player 1", "player 2"]

# --- СУЧАСНИЙ LIFESPAN МЕНЕДЖЕР ДЛЯ FASTAPI ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("Підключення до бази даних Supabase PostgreSQL...")
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    webhook_url = f"{BASE_URL}/webhook"
    logger.info(f"Встановлення Інтернет-вебхука: {webhook_url}")
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    
    yield
    
    logger.info("Зупинка сервісу: закриття пулу БД та видалення вебхука...")
    if db_pool:
        await db_pool.close()
    await bot.delete_webhook()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# --- ЛОГІКА ЗАПУСКУ ТА НАДСИЛАННЯ ПОВІДОМЛЕНЬ ---

async def send_rules_message(chat_id: int):
    """Надсилає ПОСТ 'ПРАВИЛА' з кнопками"""
    if RULES_PHOTO_URL:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=RULES_PHOTO_URL,
                caption=get_welcome_text(),
                parse_mode="HTML",
                reply_markup=get_free_rules_keyboard()
            )
            return
        except Exception as e:
            logger.error(f"Помилка надсилання фото: {e}")

    await bot.send_message(
        chat_id=chat_id,
        text=get_welcome_text(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_free_rules_keyboard()
    )

async def start_new_game_10_flow(chat_id: int):
    """Ініціалізація гри та виведення ПОСТ 'ЗАВДАННЯ 1' без кнопок"""
    p1, p2 = await get_chat_players_tags(chat_id)
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO games (chat_id, current_round, max_rounds, scores) "
            "VALUES ($1, 1, 10, '{}'::jsonb) "
            "ON CONFLICT (chat_id) DO UPDATE SET current_round = 1, max_rounds = 10, scores = '{}'::jsonb",
            chat_id
        )
    
    task1_text = (
        "Рахунок\n"
        f"{p1}: 0\n"
        f"{p2}: 0\n\n"
        "Завдання: 1\n\n"
        "Знайди і сфотографуй число 1."
    )
    # Зверни увагу: для Завдання 1 reply_markup НЕ передається (відповідно до ТЗ)
    await bot.send_message(chat_id=chat_id, text=task1_text)

# --- ОБРОБКА КОМАНД ТА ПОДІЙ ---

@dp.message(Command("start", "play"))
async def cmd_start(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        await send_rules_message(message.chat.id)
    else:
        await message.answer(
            "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). "
            "Знайдеш мене через пошук @stophotobot"
        )

@dp.message(F.new_chat_members)
async def on_bot_added_as_member(message: types.Message):
    for member in message.new_chat_members:
        if member.id == message.bot.id:
            logger.info(f"Бота додано в групу {message.chat.id}")
            await send_rules_message(message.chat.id)

# --- МЕХАНІКА ОБРОБКИ ІГРОВИХ ФОТОГРАФІЙ ---

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    chat_id = message.chat.id
    if message.chat.type not in ["group", "supergroup"]:
        return

    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT current_round, max_rounds, scores FROM games WHERE chat_id = $1", chat_id)
        if not game:
            return

        current_round = game['current_round']
        max_rounds = game['max_rounds']
        scores = json.loads(game['scores']) if isinstance(game['scores'], str) else dict(game['scores'])

        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        scores[username] = scores.get(username, 0) + 1
        
        # Кінець гри (Раунд 10 пройдено)
        if current_round >= max_rounds:
            await conn.execute("UPDATE games SET current_round = $2, scores = $3 WHERE chat_id = $1", chat_id, current_round, json.dumps(scores))
            score_text = "\n".join([f"{u}: {s}" for u, s in scores.items()])
            winner = max(scores, key=scores.get)
            
            end_text = f"Рахунок\n{score_text}\n\nПереможець: {winner}\n\nНе забудь про свій приз!"
            await message.answer(end_text, reply_markup=get_free_game_keyboard(current_round + 1))
            return

        # Перехід на ПОСТ "ЗАВДАННЯ 2-10"
        next_round = current_round + 1
        await conn.execute("UPDATE games SET current_round = $2, scores = $3 WHERE chat_id = $1", chat_id, next_round, json.dumps(scores))
        
        score_text = "\n".join([f"{u}: {s}" for u, s in scores.items()])
        round_msg = f"Рахунок\n{score_text}\n\nЗавдання: {next_round}"
        await message.answer(round_msg, reply_markup=get_free_game_keyboard(next_round))

# --- ОБРОБКА CALLBACK КНОПОК ---

@dp.callback_query(F.data == "start_game_10")
async def callback_start_game_10(callback: types.CallbackQuery):
    await callback.answer()
    await start_new_game_10_flow(callback.message.chat.id)

@dp.callback_query(F.data.startswith("cancel_round_"))
async def cancel_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    try:
        target_round = int(callback.data.split("_")[-1])
    except ValueError:
        return

    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT current_round, scores FROM games WHERE chat_id = $1", chat_id)
        if not game or game['current_round'] - 1 != target_round:
            await callback.answer("Цей раунд вже не можна обнулити!", show_alert=True)
            return

        scores = json.loads(game['scores']) if isinstance(game['scores'], str) else dict(game['scores'])
        username = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name

        if username in scores and scores[username] > 0:
            scores[username] -= 1

        await conn.execute("UPDATE games SET current_round = $2, scores = $3 WHERE chat_id = $1", chat_id, target_round, json.dumps(scores))

    await callback.answer(f"Раунд {target_round} обнулено!")
    score_text = "\n".join([f"{u}: {s}" for u, s in scores.items()])
    
    if target_round == 1:
        # Повернення на Завдання 1 (без кнопок)
        msg_text = f"Рахунок\n{score_text}\n\nЗавдання: 1\n\nЗнайди і сфотографуй число 1."
        await callback.message.answer(msg_text)
    else:
        msg_text = f"Рахунок\n{score_text}\n\nЗавдання: {target_round}"
        await callback.message.answer(msg_text, reply_markup=get_free_game_keyboard(target_round))

@dp.callback_query(F.data == "buy_pro")
async def callback_buy_pro(callback: types.CallbackQuery):
    await callback.answer()
    pro_text = (
        "Pro-версія гри:\n"
        "- до 10 гравців\n"
        "- до 100 раундів назавжди\n"
        "- у всіх чатах Pro-гравця"
    )
    # Кнопки для поста ОПЛАТА
    builder = InlineKeyboardBuilder()
    builder.button(text="КУПИТИ PRO-ВЕРСІЮ", url="https://monobank.ua") # Заглушка посилання оплати
    builder.button(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="start_game_10")
    builder.adjust(1)
    await callback.message.answer(pro_text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "add_players")
async def callback_add_players(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "👥 <b>Як додати гравців?</b>\n\n"
        "Просто запросіть друзів у цей чат! Кожен, хто надішле фото з потрібним числом, автоматично потрапить у загальний рахунок гри."
    )

# --- ЕНДПОІНТИ FastAPI ДЛЯ РЕНДЕРУ ---

@app.post("/webhook")
async def webhook(request: Request):
    json_str = await request.body()
    update = Update.model_validate_json(json_str)
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "working", "info": "100 PHOTO Bot Online"}
