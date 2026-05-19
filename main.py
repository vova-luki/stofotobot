import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatType, ContentType
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg

# --- ЛОГУВАННЯ ТА КОНФІГУРАЦІЯ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Отримуємо залізобетонні змінні оточення з Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_PATH = f"/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# Адмін-дані
ADMIN_ID = 124303561  # Vova Luki

# File IDs або URL-адреси твоїх трьох картинок (заміни на реальні file_id після першого завантаження)
PHOTO_RULES = "https://stophotobot.onrender.com/static/1.png"  # Або File ID твого 1.png
PHOTO_START = "https://stophotobot.onrender.com/static/2.png"  # Або File ID твого 2.png
PHOTO_END = "https://stophotobot.onrender.com/static/3.png"    # Або File ID твого 3.png

# Посилання на оплату (Monobank)
MONOBANK_URL = "https://send.monobank.ua/jar/example"  # Твоя банка з параметрами еквайрингу

# Ініціалізація aiogram
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальний пул з'єднань з БД
db_pool = None

# --- СУЧАСНИЙ LIFESPAN ДЛЯ FASTAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("Старт додатка: ініціалізація пулу БД та Webhook")
    
    # Підключення до Supabase через Connection Pooler (порт 6543)
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)
    
    # Встановлення вебхука в Telegram
    await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    
    yield
    
    # Закриття ресурсів
    await bot.delete_webhook()
    await db_pool.close()
    logger.info("Зупинка додатка: пул БД закрито, вебхук видалено")

app = FastAPI(lifespan=lifespan)

# --- ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ РОБОТИ З БД ---
async def get_game(chat_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM games WHERE chat_id = $1", chat_id)
        if row:
            data = dict(row)
            data['scores'] = json.loads(data['scores'])
            data['history'] = json.loads(data['history'])
            return data
        return None

async def save_game(chat_id: int, state: str, max_rounds: int, current_round: int, scores: dict, history: list):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO games (chat_id, state, max_rounds, current_round, scores, history)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (chat_id) DO UPDATE 
            SET state = $2, max_rounds = $3, current_round = $4, scores = $5, history = $6
            """,
            chat_id, state, max_rounds, current_round, json.dumps(scores), json.dumps(history)
        )

async def check_pro_in_chat(chat_id: int) -> bool:
    """Перевіряє, чи є хоча б один PRO-користувач серед тих, хто вже брав участь, або у чаті."""
    # Оскільки Telegram API не дає список усіх юзерів групи, ми орієнтуємося на учасників гри 
    # та перевіряємо, чи є в чаті PRO-ініціатори. Також адмін може додавати їх вручну.
    async with db_pool.acquire() as conn:
        game = await get_game(chat_id)
        if game and game['scores']:
            user_ids = [int(uid) for uid in game['scores'].keys()]
            if user_ids:
                records = await conn.fetch("SELECT is_pro FROM users WHERE telegram_id = ANY($1)", user_ids)
                if any(r['is_pro'] for r in records):
                    return True
        return False

async def is_user_pro(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        res = await conn.fetchval("SELECT is_pro FROM users WHERE telegram_id = $1", user_id)
        return bool(res)

async def ensure_user_exists(user_id: int, username: str, full_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE SET username = $2, full_name = $3
            """,
            user_id, username, full_name
        )

# --- ГЕНЕРАЦІЯ ТЕКСТІВ ТА КНОПОК ---
def get_user_mention(user_data: dict) -> str:
    return user_data.get('name') or f"@{user_data.get('username', 'user')}"

def format_scores(scores: dict) -> str:
    lines = []
    for uid, udata in scores.items():
        lines.append(f"{get_user_mention(udata)}: {udata['score']}")
    return "\n".join(lines)

async def build_game_view(chat_id: int, game: dict):
    is_pro = await check_pro_in_chat(chat_id)
    builder = InlineKeyboardBuilder()
    
    if game['state'] == 'PLAYING':
        if game['current_round'] == 1:
            text = f"Завдання: 1\n\nРахунок\n{format_scores(game['scores'])}\n\nЗнайди і сфотографуй число 1."
            # Пост Завдання 1 за твоїм ТЗ йде без кнопок під текстом
            return text, None
        else:
            text = f"Завдання: {game['current_round']}\n\n{format_scores(game['scores'])}"
            builder.button(text=f"[ ОБНУЛИТИ РАУНД {game['current_round']-1} ]", callback_data=f"rollback_{chat_id}")
            if is_pro:
                builder.button(text="[ ПОЧАТИ ЗАНОВО ]", callback_data=f"restart_{chat_id}")
            else:
                builder.button(text="[ НОВА ГРА ДО 10 ]", callback_data=f"new10_{chat_id}")
                builder.button(text="[ НОВА ГРА ДО 100 ]", callback_data=f"pay_{chat_id}")
                builder.button(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data=f"pay_{chat_id}")
            builder.adjust(1)
            return text, builder.as_markup()
            
    return "", None

# --- ОБРОБКА КОМАНД СТАРТУ ТА ОНОВЛЕННЯ ЧАТУ ---
async def process_start_or_join(chat_id: int, member_count: int):
    # Оскільки бот не рахується, віднімаємо 1
    players_count = member_count - 1 if member_count > 1 else 1
    is_pro = await check_pro_in_chat(chat_id)
    
    builder = InlineKeyboardBuilder()
    
    if players_count == 1:
        text = "Щоб грати, додайте в групу другого гравця.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
        builder.button(text="[ НОВА ГРА ДО 10 ]", callback_data=f"new10_{chat_id}")
        await bot.send_message(chat_id, text, reply_markup=builder.as_markup())
        await save_game(chat_id, "SETTING_UP", 10, 1, {}, [])
        
    elif not is_pro and players_count == 2:
        text = "Вітаємо у грі <a href='https://t.me/stophotobot'>100 PHOTO</a>!\nПравила гри:\n\n1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 фото.\nЗа кожне фото гравець отримує 1 бал.\n\n3. Числа не можна створювати (викладати предметами) або писати самому.\nЛише фотографувати їх вдома, на вулиці тощо.\n\n4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо).\nЛокації мають бути різними.\n\n5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\nЩоб перезапустити бота, напишіть в чат команду /start або /play.\n\nЗа бажанням, придумайте приз переможцю.\n\nНатхнення!"
        builder.button(text="[ НОВА ГРА ДО 10 ]", callback_data=f"startgame_10_{chat_id}")
        builder.button(text="[ НОВА ГРА ДО 100 ]", callback_data=f"pay_{chat_id}")
        builder.button(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data=f"pay_{chat_id}")
        builder.adjust(1)
        await bot.send_photo(chat_id, photo=PHOTO_RULES, caption=text, parse_mode="HTML", reply_markup=builder.as_markup())
        await save_game(chat_id, "SETTING_UP", 10, 1, {}, [])
        
    elif not is_pro and players_count >= 3:
        text = "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\nPro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах Pro-гравця"
        builder.button(text="[ КУПИТИ PRO-ВЕРСІЮ ]", url=MONOBANK_URL)
        await bot.send_message(chat_id, text, reply_markup=builder.as_markup())
        
    elif is_pro and (2 <= players_count <= 10):
        text = "Вітаємо у грі <a href='https://t.me/stophotobot'>100 PHOTO</a>!\nПравила гри:\n\n1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 фото.\nЗа кожне фото гравець отримує 1 бал.\n\n3. Числа не можна створювати (викладати предметами) або писати самому.\nЛише фотографувати їх вдома, на вулиці тощо.\n\n4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо).\nЛокації мають бути різними.\n\n5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\nЩоб перезапустити бота, напишіть в чат команду /start або /play.\n\nЗа бажанням, придумайте приз переможцю.\n\nНатхнення!"
        builder.button(text="[ НОВА ГРА ]", callback_data=f"startgame_100_{chat_id}")
        await bot.send_photo(chat_id, photo=PHOTO_RULES, caption=text, parse_mode="HTML", reply_markup=builder.as_markup())
        await save_game(chat_id, "SETTING_UP", 100, 1, {}, [])
        
    elif is_pro and players_count > 10:
        text = "На жаль, грати може максимум 10 гравців.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
        builder.button(text="[ НАС ВЖЕ 10 ]", callback_data=f"check_members_{chat_id}")
        await bot.send_message(chat_id, text, reply_markup=builder.as_markup())

@dp.message(Command("start", "play"))
async def cmd_start(message: types.Message):
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        count = await message.chat.get_member_count()
        await process_start_or_join(message.chat.id, count)

# Динамічний тригер на додавання нових людей в групу
@dp.message(F.new_chat_members)
async def on_user_join(message: types.Message):
    count = await message.chat.get_member_count()
    await process_start_or_join(message.chat.id, count)

# --- ХЕНДЛЕРИ ДЛЯ ТРИГЕРІВ КНОПОК (CALLBACK QUERIES) ---
@dp.callback_query(F.data.startswith("startgame_"))
async def start_game_flow(callback: types.CallbackQuery):
    _, rounds_str, chat_id_str = callback.data.split("_")
    chat_id = int(chat_id_str)
    max_rounds = int(rounds_str)
    
    game = await get_game(chat_id) or {"scores": {}, "history": []}
    
    await save_game(chat_id, "PLAYING", max_rounds, 1, game["scores"], [])
    
    text = f"Завдання: 1\n\nРахунок\n{format_scores(game['scores']) if game['scores'] else '@user1: 0\n@user2: 0'}\n\nЗнайди і сфотографуй число 1."
    await bot.send_photo(chat_id, photo=PHOTO_START, caption=text)
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def show_payment(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    builder = InlineKeyboardBuilder()
    builder.button(text="[ КУПИТИ PRO-ВЕРСІЮ ]", url=MONOBANK_URL)
    builder.button(text="[ ПРОДОВЖИТИ ГРУ УДВОХ ]", callback_data=f"new10_{chat_id}")
    builder.adjust(1)
    
    text = "Pro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах Pro-гравця"
    await bot.send_message(chat_id, text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("new10_"))
async def force_new_10(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    await process_start_or_join(chat_id, 3) # Симулюємо стандартну перевірку на 2 особи
    await callback.answer()

@dp.callback_query(F.data.startswith("restart_"))
async def force_restart(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    game = await get_game(chat_id)
    if game:
        await save_game(chat_id, "PLAYING", game["max_rounds"], 1, {}, [])
        text = "Завдання: 1\n\nРахунок\n@user1: 0\n@user2: 0\n\nЗнайди і сфотографуй число 1."
        await bot.send_photo(chat_id, photo=PHOTO_START, caption=text)
    await callback.answer()

@dp.callback_query(F.data.startswith("rollback_"))
async def rollback_round(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    game = await get_game(chat_id)
    
    if game and game["state"] == "PLAYING" and game["current_round"] > 1:
        last_round_idx = game["current_round"] - 2
        if 0 <= last_round_idx < len(game["history"]):
            last_user_id = str(game["history"].pop())
            if last_user_id in game["scores"]:
                game["scores"][last_user_id]["score"] = max(0, game["scores"][last_user_id]["score"] - 1)
            game["current_round"] -= 1
            
            await save_game(chat_id, "PLAYING", game["max_rounds"], game["current_round"], game["scores"], game["history"])
            text, reply_markup = await build_game_view(chat_id, game)
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("check_members_"))
async def check_members_btn(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    count = await bot.get_chat_member_count(chat_id)
    await process_start_or_join(chat_id, count)
    await callback.answer()

# --- ОБРОБНИК СТРАТЕГІЧНИХ ФОТОГРАФІЙ ---
@dp.message(F.content_type == ContentType.PHOTO)
async def handle_photo(message: types.Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    chat_id = message.chat.id
    game = await get_game(chat_id)
    
    if not game or game["state"] != "PLAYING":
        return

    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or message.from_user.username
    username = message.from_user.username or ""
    
    await ensure_user_exists(message.from_user.id, username, message.from_user.full_name)
    
    # Ініціалізація юзера в скорборді, якщо новий
    if user_id not in game["scores"]:
        game["scores"][user_id] = {"name": user_name, "username": username, "score": 0}
        
    # Зараховуємо бал за поточний раунд
    game["scores"][user_id]["score"] += 1
    game["history"].append(int(user_id))
    
    # Перевірка на фінал гри
    if game["current_round"] >= game["max_rounds"]:
        game["state"] = "FINISHED"
        winner_id = max(game["scores"], key=lambda k: game["scores"][k]["score"])
        winner_mention = get_user_mention(game["scores"][winner_id])
        
        text = f"Переможець: {winner_mention}\n\nРахунок:\n{format_scores(game['scores'])}\n\nНе забудь про свій приз!"
        
        builder = InlineKeyboardBuilder()
        builder.button(text=f"[ ОБНУЛИТИ РАУНД {game['max_rounds']} ]", callback_data=f"rollback_{chat_id}")
        
        is_pro = await check_pro_in_chat(chat_id)
        if is_pro:
            builder.button(text="[ НОВА ГРА ]", callback_data=f"startgame_100_{chat_id}")
        else:
            builder.button(text="[ НОВА ГРА ДО 10 ]", callback_data=f"startgame_10_{chat_id}")
            builder.button(text="[ НОВА ГРА ДО 100 ]", callback_data=f"pay_{chat_id}")
            builder.button(text="[ ДОДАТИ УЧАСНИКА ]", callback_data=f"pay_{chat_id}")
        builder.adjust(1)
        
        await bot.send_photo(chat_id, photo=PHOTO_END, caption=text, reply_markup=builder.as_markup())
        await save_game(chat_id, "FINISHED", game["max_rounds"], game["current_round"], game["scores"], game["history"])
    else:
        # Перехід на наступний раунд
        game["current_round"] += 1
        await save_game(chat_id, "PLAYING", game["max_rounds"], game["current_round"], game["scores"], game["history"])
        
        text, reply_markup = await build_game_view(chat_id, game)
        if reply_markup:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id, text)

# --- АДМІН-КОМАНДА /stat (ОПТИМІЗОВАНА ЧЕРЕЗ GATHER) ---
@dp.message(Command("stat"))
async def admin_stat(message: types.Message):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        now = datetime.utcnow()

        async def get_metrics(delta_days=None):
            if delta_days:
                time_filter = now - timedelta(days=delta_days)
                q_chats = "SELECT COUNT(*) FROM games WHERE created_at >= $1"
                q_users = "SELECT COUNT(*) FROM users WHERE created_at >= $1"
                q_free = "SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= $1"
                q_pro = "SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= $1"
                
                return await asyncio.gather(
                    conn.fetchval(q_chats, time_filter),
                    conn.fetchval(q_users, time_filter),
                    conn.fetchval(q_free, time_filter),
                    conn.fetchval(q_pro, time_filter)
                )
            else:
                return await asyncio.gather(
                    conn.fetchval("SELECT COUNT(*) FROM games"),
                    conn.fetchval("SELECT COUNT(*) FROM users"),
                    conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = FALSE"),
                    conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE")
                )

        # Паралельне виконання агрегацій для оптимізації
        all_time, m30, m7, h24 = await asyncio.gather(
            get_metrics(None),
            get_metrics(30),
            get_metrics(7),
            get_metrics(1)
        )

        stat_text = f"""
ЗА ВСІЙ ЧАС:
- всі чати: {all_time[0]}
- всі юзери: {all_time[1]}
- free-юзери: {all_time[2]}
- pro-юзери: {all_time[3]}

ЗА 30 ДНІВ:
- всі чати: {m30[0]}
- всі юзери: {m30[1]}
- free-юзери: {m30[2]}
- pro-юзери: {m30[3]}

ЗА 7 ДНІВ:
- всі чати: {m7[0]}
- всі юзери: {m7[1]}
- free-юзери: {m7[2]}
- pro-юзери: {m7[3]}

ЗА 24 ГОД:
- всі чати: {h24[0]}
- всі юзери: {h24[1]}
- free-юзери: {h24[2]}
- pro-юзери: {h24[3]}
"""
        await message.answer(stat_text.strip())

# --- РУЧНЕ ДОДАВАННЯ PRO-ЮЗЕРІВ АДМІНОМ ---
@dp.message(Command("grant_pro"))
async def grant_pro(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO users (telegram_id, is_pro) VALUES ($1, TRUE) ON CONFLICT (telegram_id) DO UPDATE SET is_pro = TRUE", target_id)
        await message.answer(f"Користувачу {target_id} успішно надано статус PRO!")
    except Exception as e:
        await message.answer("Формат команди: /grant_pro [USER_TELEGRAM_ID]")

# --- ЕНДПОІНТ ДЛЯ WEBHOOK ТА ОБРОБКИ ОПЛАТИ ---
@app.post(WEBHOOK_PATH)
async def webhook_endpoint(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)

# Ендпоінт для Monobank Webhook (приймає успішну транзакцію)
@app.post("/monobank-webhook")
async def monobank_webhook(request: Request):
    data = await request.json()
    
    # Припустимо, Monobank передає інформацію про оплату в об'єкті
    # Тобі потрібно буде розпарсити їхній респонс. Наприклад:
    status = data.get("status")
    amount = data.get("amount", 0)  # в копійках
    user_id = int(data.get("ccard") or 0) # Або зчитати переданий custom параметр (ID користувача)
    
    if status == "success" and amount >= 10000:  # 100 грн і більше
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_pro = TRUE WHERE telegram_id = $1", user_id)
            
            # Шукаємо останню активну гру користувача для активації PRO на льоту
            row = await conn.fetchrow("SELECT chat_id FROM games WHERE state = 'SETTING_UP'")
            if row:
                chat_id = row['chat_id']
                builder = InlineKeyboardBuilder()
                builder.button(text="[ НОВА ГРА ]", callback_data=f"startgame_100_{chat_id}")
                
                success_text = "Дякую, оплата є!\n– @user тепер Pro\n– відкрито 100 раундів\n– відкрито 10 гравців"
                await bot.send_message(chat_id, success_text, reply_markup=builder.as_markup())
                
    return Response(status_code=200)
