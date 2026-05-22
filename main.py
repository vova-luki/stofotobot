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
DB_POOL = None

# --- БАЗА ДАНИХ ТА ДОПОМІЖНІ ФУНКЦІЇ ---

async def init_db():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Пул підключень до БД успішно створено.")
        
        async with DB_POOL.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS games (
                    chat_id BIGINT PRIMARY KEY,
                    status TEXT,
                    round_number INT,
                    players JSONB,
                    current_word_data JSONB
                );
                CREATE TABLE IF NOT EXISTS pro_users (
                    user_id BIGINT PRIMARY KEY,
                    is_pro BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            logger.info("Таблиці в БД перевірено/створено.")

async def init_or_get_game(chat_id: int) -> dict:
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT status, round_number, players FROM games WHERE chat_id = $1", chat_id)
        if row:
            return {
                "status": row["status"],
                "round_number": row["round_number"],
                "players": json.loads(row["players"])
            }
        else:
            default_players = {}
            await conn.execute(
                "INSERT INTO games (chat_id, status, round_number, players, current_word_data) VALUES ($1, $2, $3, $4, $5)",
                chat_id, "registration", 0, json.dumps(default_players), None
            )
            return {"status": "registration", "round_number": 0, "players": default_players}

async def save_game(chat_id: int, status: str, round_number: int, players: dict):
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            "UPDATE games SET status = $1, round_number = $2, players = $3 WHERE chat_id = $4",
            status, round_number, json.dumps(players), chat_id
        )

async def is_user_pro(user_id: int) -> bool:
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT is_pro FROM pro_users WHERE user_id = $1", user_id)
        return row["is_pro"] if row else False

async def set_user_pro_status(user_id: int, status: bool):
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO pro_users (user_id, is_pro, updated_at) VALUES ($1, $2, CURRENT_TIMESTAMP) "
            "ON CONFLICT (user_id) DO UPDATE SET is_pro = $2, updated_at = CURRENT_TIMESTAMP",
            user_id, status
        )

async def update_player_name_background(chat_id: int, user: types.User):
    if not user or user.is_bot:
        return
        
    game = await init_or_get_game(chat_id)
    players = game["players"]
    uid = str(user.id)
    
    current_name = f"@{user.username}" if user.username else user.first_name
    
    if uid in players:
        if players[uid].get("name") != current_name:
            players[uid]["name"] = current_name
            await save_game(chat_id, game["status"], game["round_number"], players)
    else:
        players[uid] = {"name": current_name, "score": 0}
        await save_game(chat_id, game["status"], game["round_number"], players)

def format_scoreboard(players: dict, default_count: int = 2) -> str:
    lines = []
    active_players = list(players.items())
    
    # Використовуємо або реальних гравців, або дефолтну кількість заглушок
    slots = max(default_count, len(active_players))
    
    for i in range(slots):
        if i < len(active_players):
            uid, pdata = active_players[i]
            lines.append(f"{pdata['name']}: {pdata['score']}")
        else:
            lines.append(f"player {i+1}: 0")
    return "\n".join(lines)

# --- ВІДПРАВКА СТАРТОВИХ ПОСТІВ ТА ПЕРЕВІРКА КІЛЬКОСТІ ЛУДЕЙ ---

async def send_welcome_rules(chat_id: int):
    try:
        count = await bot.get_chat_member_count(chat_id)
        players_count = count - 1  # Мінус сам бот
    except TelegramAPIError as e:
        logger.error(f"Помилка отримання кількості учасників у чаті {chat_id}: {e}")
        players_count = 2  # Дефолт для безпеки

    try:
        if players_count == 1:
            text = (
                "Щоб грати, додайте в групу другого гравця.\n\n"
                "Щоб перезапустити бота, напишіть в чат команду /start або /play."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")]
            ])
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return

        if players_count >= 3 and players_count <= 10:
            # Перевіряємо чи є в чаті хтось з PRO статусом (для спрощення перевіряємо поточний статус чату або залишаємо вибір)
            text = (
                "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n\n"
                "Pro-версія гри:\n"
                "- до 10 гравців\n"
                "- до 100 раундів назавжди\n"
                "- у всіх чатах Pro-гравця"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="buy_pro_version")]
            ])
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return

        if players_count > 10:
            text = (
                "На жаль, грати може максимум 10 гравців.\n\n"
                "Щоб перезапустити бота, напишіть в чат команду /start або /play."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="start_game_pro_max")]
            ])
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return

        # Стандартний пост "ПРАВИЛА" для 2 людей
        text = (
            "Вітаємо у <a href=\"https://t.me/stophotobot\">100 PHOTO</a>!\n"
            "Правила гри:\n\n"
            "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат. 1 раунд = 1 photo.\n"
            "2. За кожне фото гравець отримує 1 бал. Безоплатна гра триває 10 раундів, платна – 100 раундів.\n"
            "3. Числа не можна створювати (викладати предметами) або писати самому. Лише фотографувати їх вдома, на вулиці тощо.\n"
            "4. Не можна брати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо). Локації мають бути різними.\n\n"
            "5. Якщо надіслане фото не відповідає правилам, це photo можна відмінити і почати раунд заново.\n"
            "Щоб перезапустити бота, напишіть у чат команду /start або /play.\n\n"
            "За бажанням, придумайте приз переможцю.\n\n"
            "Натхнення!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="buy_pro_version")],
            [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="buy_pro_version")]
        ])
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramAPIError as e:
        logger.error(f"Помилка відправки повідомлення в чат {chat_id}: {e}")

# --- ОСОБИСТІ ПОВІДОМЛЕННЯ (ЗАГЛУШКА / АДМІН) ---

async def private_chat_handler(message: types.Message):
    if message.from_user.id == 124303561 and message.text == "/stat":
        return  # Окремо обробиться у хендлері команди /stat
        
    text = (
        "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу).\n\n"
        "Знайдеш мене через пошук – @stophotobot"
    )
    await message.answer(text)

# --- ОБРОБНИКИ КОМАНД ТА ПОДІЙ ---

@dp.message(Command("stat"), F.chat.type == "private")
async def admin_stat_command(message: types.Message):
    if message.from_user.id != 124303561:
        await private_chat_handler(message)
        return
    # Проста адмін-статистика (за потреби можна розширити паралельними запитами)
    async with DB_POOL.acquire() as conn:
        total_games = await conn.fetchval("SELECT COUNT(*) FROM games")
        total_pro = await conn.fetchval("SELECT COUNT(*) FROM pro_users WHERE is_pro = TRUE")
    
    text = f"📊 <b>СТАТИСТИКА БОТА:</b>\n\nВсього ігор створено: {total_games}\nВсього PRO користувачів: {total_pro}"
    await message.answer(text)

@dp.message(Command("start", "play"))
async def reset_game_command(message: types.Message):
    chat_id = message.chat.id
    if message.chat.type == "private":
        await private_chat_handler(message)
        return
        
    await update_player_name_background(chat_id, message.from_user)
    game = await init_or_get_game(chat_id)
    players = game["players"]
    
    if players:
        for uid in players:
            players[uid]["score"] = 0
            
    await save_game(chat_id, "registration", 0, players)
    await send_welcome_rules(chat_id)

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added_to_group(event: types.ChatMemberUpdated):
    chat_id = event.chat.id
    if event.chat.type in ["group", "supergroup"]:
        await save_game(chat_id, "registration", 0, {})
        await send_welcome_rules(chat_id)

# Фонове перехоплення імен з абсолютно будь-якої активності тексту/стікерів у групі
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def background_activity_catcher(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    await update_player_name_background(message.chat.id, message.from_user)

# --- ОБРОБКА CALLBACK КНОПОК ---

@dp.callback_query(F.data == "start_game_10")
async def process_start_game_10(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await update_player_name_background(chat_id, callback.from_user)
    
    game = await init_or_get_game(chat_id)
    players = game["players"]
    
    await save_game(chat_id, "playing", 1, players)
    score_text = format_scoreboard(players, default_count=2)
    
    text = (
        "Раунд 1.\n\n"
        "Рахунок\n"
        f"{score_text}\n\n"
        "Завдання: сфотографуй число 1."
    )
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "buy_pro_version")
async def process_buy_pro(callback: types.CallbackQuery):
    pay_url = f"https://send.monobank.ua/jar/YOUR_JAR_ID?a=100&c={callback.from_user.id}"
    text = (
        "Pro-версія гри:\n"
        "- до 10 гравців\n"
        "- до 100 раундів назавжди\n"
        "- у всіх чатах Pro-гравця"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=pay_url)],
        [InlineKeyboardButton(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="start_game_10")]
    ])
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("clear_round_"))
async def process_clear_round(callback: types.CallbackQuery):
    # Логіка скасування раунду назад (крок назад)
    chat_id = callback.message.chat.id
    target_round = int(callback.data.split("_")[-1])
    
    game = await init_or_get_game(chat_id)
    players = game["players"]
    
    await save_game(chat_id, "playing", target_round, players)
    score_text = format_scoreboard(players, default_count=2)
    
    text = (
        f"Раунд {target_round}\n\n"
        "Рахунок\n"
        f"{score_text}\n\n"
        f"Завдання: число {target_round}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {target_round-1}", callback_data=f"clear_round_{target_round-1}")],
        [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")]
    ]) if target_round > 1 else None
    
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

# --- ГОЛОВНИЙ ІГРОВИЙ ХЕНДЛЕР: ОБРОБКА ФОТОГРАФІЙ ---

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
async def process_game_photo(message: types.Message):
    chat_id = message.chat.id
    await update_player_name_background(chat_id, message.from_user)
    
    game = await init_or_get_game(chat_id)
    if game["status"] != "playing":
        return
        
    round_number = game["round_number"]
    players = game["players"]
    uid_str = str(message.from_user.id)
    
    # Нараховуємо бал тому, хто перший надіслав фото
    if uid_str in players:
        players[uid_str]["score"] += 1
    else:
        current_name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        players[uid_str] = {"name": current_name, "score": 1}
        
    next_round = round_number + 1
    max_rounds = 10  # У безкоштовній версії за замовчуванням
    
    score_text = format_scoreboard(players, default_count=2)
    
    if round_number >= max_rounds:
        # Фінал гри
        await save_game(chat_id, "finished", round_number, players)
        winner_name = players[uid_str]["name"]
        
        text = (
            f"Переможець: {winner_name}\n\n"
            "Рахунок\n"
            f"{score_text}\n\n"
            "Не забудь про свій приз!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {round_number}", callback_data=f"clear_round_{round_number}")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="buy_pro_version")],
            [InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="buy_pro_version")]
        ])
        await message.answer(text, reply_markup=kb)
    else:
        # Наступний раунд
        await save_game(chat_id, "playing", next_round, players)
        text = (
            f"Раунд {next_round}\n\n"
            "Рахунок\n"
            f"{score_text}\n\n"
            f"Завдання: число {next_round}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {next_round-1}", callback_data=f"clear_round_{next_round-1}")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")]
        ])
        await message.answer(text, reply_markup=kb)

# --- WEBHOOK FASTAPI СЕРВЕР ТА ЛІФСПАН ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    yield
    # Акуратне закриття всіх конекторів та сесій
    await bot.delete_webhook()
    session = await bot.get_session()
    if session and not session.closed:
        await session.close()
    if DB_POOL:
        await DB_POOL.close()
        logger.info("Пул підключень до БД успішно закрито.")

app = FastAPI(lifespan=lifespan)

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
        
    update = types.Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)

@app.post("/monobank")
async def monobank_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
        
    if data.get("type") == "StatementItem":
        statement = data.get("data", {}).get("statementItem", {})
        comment = statement.get("comment", "")
        amount = statement.get("amount", 0)
        
        # Перевірка на суму від 100 грн (вхідні дані в копійках: 10000 = 100 грн)
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
                        "Дякую, оплата є!\n"
                        f"– {u_name} тепер Pro\n"
                        "– відкрито 100 раундів\n"
                        "– відкрито 10 гравців"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game_10")]
                    ])
                    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
                except TelegramAPIError:
                    pass
                    
    return Response(status_code=200)

@app.get("/")
async def root_health_check():
    return {"status": "healthy", "game": "100 PHOTO"}
