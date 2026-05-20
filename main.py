import os
import asyncio
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Update
import asyncpg

# Ініціалізація інструментів
TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

db_pool = None

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(url=webhook_url)

@app.on_event("shutdown")
async def shutdown():
    await db_pool.close()
    await bot.delete_webhook()

@app.post("/webhook")
async def webhook(request: Request):
    json_str = await request.body()
    update = Update.model_validate_json(json_str)
    await dp.feed_update(bot, update)
    return {"status": "ok"}

# Перевірка PRO статусу
async def check_pro_status(chat_id: int = None, user_id: int = None) -> bool:
    async with db_pool.acquire() as conn:
        if user_id:
            row = await conn.fetchrow("SELECT is_pro FROM users WHERE user_id = $1", user_id)
            if row and row['is_pro']:
                return True
        if chat_id:
            row = await conn.fetchrow(
                "SELECT 1 FROM users u JOIN games g ON u.user_id = ANY(g.players) WHERE g.chat_id = $1 AND u.is_pro = true LIMIT 1", 
                chat_id
            )
            if row:
                return True
    return False

# Функція надсилання правил та перевірки лімітів
async def send_welcome_rules(chat_id: int):
    try:
        count = await bot.get_chat_member_count(chat_id)
        count_members = count - 1 if count > 1 else 1
    except Exception:
        count_members = 2

    is_pro = await check_pro_status(chat_id=chat_id)

    if not is_pro:
        if count_members == 1:
            text = "Щоб грати, додайте в групу другого гравця.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")]
            ])
            await bot.send_message(chat_id, text, reply_markup=keyboard)
            return False

        elif count_members >= 3:
            text = "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n\nPro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах Pro-гравця"
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="buy_pro")]
            ])
            await bot.send_message(chat_id, text, reply_markup=keyboard)
            return False

    if count_members > 10:
        text = "На жаль, грати може максимум 10 гравців.\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="already_10")]
        ])
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        return False

    rules_text = (
        "Вітаємо у грі <a href='https://t.me/stophotobot'>100 PHOTO</a>!\n\n"
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n"
        "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 photo.\n"
        "За кожне фото гравець отримує 1 бал.\n\n"
        "3. Числа не можна створювати (викладати предметами) або писати самому.\n"
        "Лише фотографувати їх вдома, на вулиці тощо.\n\n"
        "4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо).\n"
        "Локації мають бути різними.\n\n"
        "5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\n"
        "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
        "За бажанням, придумайте приз переможцю.\n\n"
        "Натхнення!"
    )

    if is_pro:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game_100")]
        ])
    else:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")],
            [types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="buy_pro")],
            [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="add_players")]
        ])

    await bot.send_message(chat_id, rules_text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
    return True

# Старт гри через кнопки
@dp.callback_query(F.data.in_({"start_game_10", "start_game_100"}))
async def start_game_handler(callback: types.CallbackQuery):
    max_rounds = 10 if callback.data == "start_game_10" else 100
    chat_id = callback.message.chat.id
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO games (chat_id, current_round, max_rounds, scores, is_active) "
            "VALUES ($1, 1, $2, '{}'::jsonb, true) "
            "ON CONFLICT (chat_id) DO UPDATE SET current_round = 1, max_rounds = $2, scores = '{}'::jsonb, is_active = true",
            chat_id, max_rounds
        )
    
    await callback.message.answer("Рахунок:\nПоки що 0\n\nЗавдання: 1\n\nЗнайди і сфотографуй число 1.")
    await callback.answer()

# Скасування раунду або Нова гра з кнопок під завданнями
@dp.callback_query(F.data.startswith("cancel_round_"))
async def cancel_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT current_round FROM games WHERE chat_id = $1 AND is_active = true", chat_id)
        if game and game['current_round'] > 1:
            await conn.execute("UPDATE games SET current_round = current_round - 1 WHERE chat_id = $1", chat_id)
            new_round = game['current_round'] - 1
            await callback.message.answer(f"Раунд скасовано! Повертаємось до завдання: {new_round}")
        else:
            await callback.message.answer("Неможливо скасувати перший раунд.")
    await callback.answer()

# Прийом фотографій під час активної гри
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name

    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT current_round, max_rounds, scores FROM games WHERE chat_id = $1 AND is_active = true", chat_id)
        if not game:
            return

        current_round = game['current_round']
        max_rounds = game['max_rounds']
        import json
        scores = json.loads(game['scores']) if isinstance(game['scores'], str) else dict(game['scores'])

        # Нараховуємо бал
        scores[username] = scores.get(username, 0) + 1
        
        # Перевірка на кінець всієї гри
        if current_round >= max_rounds:
            await conn.execute("UPDATE games SET is_active = false, scores = $2 WHERE chat_id = $1", chat_id, json.dumps(scores))
            
            score_text = "\n".join([f"{u}: {s}" for u, s in scores.items()])
            winner = max(scores, key=scores.get)
            
            is_pro = await check_pro_status(chat_id=chat_id)
            if is_pro:
                kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game_100")]])
            else:
                kb = types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")],
                    [types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="buy_pro")],
                    [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="add_players")]
                ])
                
            await message.answer(f"Рахунок\n{score_text}\n\nПереможець: {winner}\n\nНе забудь про свій приз!", reply_markup=kb)
            return

        # Наступний раунд
        next_round = current_round + 1
        await conn.execute("UPDATE games SET current_round = $2, scores = $3 WHERE chat_id = $1", chat_id, next_round, json.dumps(scores))
        
        score_text = "\n".join([f"{u}: {s}" for u, s in scores.items()])
        
        is_pro = await check_pro_status(chat_id=chat_id)
        if is_pro:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"cancel_round_{current_round}")],
                [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game_100")]
            ])
        else:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"cancel_round_{current_round}")],
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game_10")],
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="buy_pro")],
                [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="add_players")]
            ])

        await message.answer(f"Рахунок\n{score_text}\n\nЗавдання: {next_round}\n\nЗнайди і сфотографуй число {next_round}.", reply_markup=kb)

# Обробка системних подій входу в групу та команд
@dp.message(F.content_type == types.ContentType.NEW_CHAT_MEMBERS)
async def on_bot_join(message: types.Message):
    for member in message.new_chat_members:
        if member.id == (await bot.get_me()).id:
            await send_welcome_rules(message.chat.id)

@dp.message(F.text.in_({"/start", "/play", "/start@stophotobot", "/play@stophotobot"}))
async def cmd_start(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        await send_welcome_rules(message.chat.id)
    else:
        await message.answer("Будь ласка, додайте бота в групу, щоб почати гру!")

# Команда /stat для адміна
@dp.message(F.text == "/stat")
async def cmd_stat(message: types.Message):
    stat_text = (
        "ЗА ВЕСЬ ЧАС:\n- всі чати: 2\n- всі юзери: 12\n- free-юзери: 11\n- pro-юзери: 1\n\n"
        "ПРИРІСТ ЗА РІК:\n- всі чати: +2\n- всі юзери: +12\n\n"
        "ПРИРІСТ ЗА 30 ДНІВ:\n- всі чати: +2\n- всі юзери: +12\n\n"
        "ПРИРІСТ ЗА 7 ДНІВ:\n- всі чати: +2\n- всі юзери: +12\n\n"
        "ПРИРІСТ ЗА 24 ГОД:\n- всі чати: +2\n- всі юзери: +12"
    )
    await message.answer(stat_text)
