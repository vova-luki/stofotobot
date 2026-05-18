import os
import logging
from fastapi import FastAPI, Request, Response, status
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton

# Налаштування логів для Render
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# База даних у пам'яті для відстеження ігор у чатах
GAMES_DATA = {} 

def get_user_name(user: types.User) -> str:
    if user.first_name:
        return user.first_name
    return f"@{user.username}" if user.username else f"ID: {user.id}"

# --- ЛОГІКА ТЕЛЕГРАМ БОТА ---

# 1. ТРИГЕР: ПІДКЛЮЧЕННЯ В ЧАТ АБО КОМАНДИ /start, /game, /play (ПРАВИЛА СУВОРO ЗА ДОКУМЕНТОМ)
@dp.message(F.new_chat_members)
@dp.message(F.text.in_({"/start", "/game", "/play", "Старт", "game", "play", "Start"}))  # Бот реагує на будь-яку з цих команд
async def on_bot_join_or_command(message: types.Message):
    # Якщо це подія входу нових учасників, реагуємо лише якщо додали нашого бота
    if message.new_chat_members:
        bot_added = any(member.id == bot.id for member in message.new_chat_members)
        if not bot_added:
            return

    # Текст повністю без змін із твого файлу
    rules_text = (
        "Вітаємо у грі <a href='https://t.me/100photobot'>100 PHOTO</a>!\n\n"
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n\n"
        "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 фото. За кожне photo гравець отримує 1 бал.\n\n"
        "3. Числа не можна створювати (викладати предметами) або писати самому. Лише фотографувати їх вдома, на вулиці тощо.\n\n"
        "4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, номери паркомісць тощо). Локації мають бути різними.\n\n"
        "5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\n\n"
        "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
        "За бажанням, придумайте приз переможцю.\n\n"
        "Натхнення!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 10 ]", callback_data="start_free")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 100 ]", callback_data="trigger_pay")],
        [InlineKeyboardButton(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data="trigger_pay")]
    ])
    
    await message.answer(rules_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

# 2. ОБРОБКА КЛІКІВ НА КНОПКИ
@dp.callback_query()
async def process_callbacks(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    
    # Кнопка: Почати безкоштовну гру до 10
    if callback.data == "start_free":
        chat_member_count = await bot.get_chat_member_count(chat_id)
        if chat_member_count > 2:
            await callback.answer("Безкоштовна гра доступна лише для 2 людей!", show_alert=True)
            await show_payment_post(chat_id)
            return
            
        GAMES_DATA[chat_id] = {
            "status": "free",
            "round": 1,
            "scores": {},
            "history": []
        }
        
        # Текст повідомлення "ЗАВДАННЯ 1" суворо за документом
        text = "Завдання: 1\n\nРахунок\nГравець 1: 0\nГравець 2: 0\n\nЗнайди і сфотографуй число 1."
        await callback.message.answer(text)
        await callback.answer()

    # Кнопка: Тригер виклику вікна оплати
    elif callback.data == "trigger_pay":
        await show_payment_post(chat_id)
        await callback.answer()
        
    # Кнопка: [ КУПИТИ PRO-ВЕРСІЮ ] (Тимчасова заглушка для перевірки Monobank)
    elif callback.data == "buy_pro":
        await callback.message.answer(
            "Обробка запиту... Платіжна система налаштовується еквайрингом."
        )
        await callback.answer()

# ПОСТ "ОПЛАТА" СУВОРO ЗА ДОКУМЕНТОМ
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

# 3. ОБРОБНИК ФОТОГРАФІЙ (ІГРОВИЙ ЦИКЛ)
@dp.message(F.photo)
async def handle_game_photo(message: types.Message):
    chat_id = message.chat.id
    
    if chat_id not in GAMES_DATA:
        return
        
    game = GAMES_DATA[chat_id]
    user_name = get_user_name(message.from_user)
    
    chat_member_count = await bot.get_chat_member_count(chat_id)
    if game["status"] == "free" and chat_member_count > 2:
        await show_payment_post(chat_id)
        return

    current_round = game["round"]
    max_rounds = 10
    
    game["scores"][user_name] = game["scores"].get(user_name, 0) + 1
    game["history"].append((user_name, current_round))
    
    # ПЕРЕВІРКА НА ФІНАЛ ГРИ
    if current_round >= max_rounds:
        winner = max(game["scores"], key=game["scores"].get)
        scores_text = "\n".join([f"{u}: {s}" for u, s in game["scores"].items()])
        
        # Текст фіналу суворо за ТЗ
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

    # НАСТУПНИЙ РАУНД
    game["round"] += 1
    next_round = game["round"]
    scores_text = "\n".join([f"{u}: {s}" for u, s in game["scores"].items()])
    
    # Структура тексту раундів суворо за документом
    task_text = f"Завдання: {next_round}\n\n{scores_text}\n\nЗнайди і сфотографуй число {next_round}."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"[ ОБНУЛИТИ РАУНД {next_round-1} ]", callback_data="cancel_last")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 10 ]", callback_data="start_free")],
        [InlineKeyboardButton(text="[ НОВА ГРА ДО 100 ]", callback_data="trigger_pay")],
        [InlineKeyboardButton(text="[ ДОДАТИ ГРАВЦІВ ]", callback_data="trigger_pay")]
    ])
        
    await message.answer(task_text, reply_markup=kb)

# 4. РЕАЛІЗАЦІЯ КНОПКИ [ ОБНУЛИТИ РАУНД ]
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
    
    scores_text = "\n".join([f"{u}: {s}" for u, s in game["scores"].items()])
    task_text = f"Раунд скасовано!\n\nЗавдання: {last_round}\n\n{scores_text}\n\nЗнайди і сфотографуй число {last_round}."
    
    await callback.message.answer(task_text)
    await callback.answer("Останній раунд скасовано!")

# 5. СУВОРЕ ІГНОРУВАННЯ БУДЬ-ЯКОГО ІНШОГО ТЕКСТУ ЗА ТЗ
@dp.message()
async def ignore_text_messages(message: types.Message):
    pass

# --- НАЛАШТУВАННЯ ВЕБХУКІВ ДЛЯ FASTAPI ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    json_str = await request.json()
    update = Update.model_validate(json_str, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "working", "info": "100 PHOTO Bot"}

@app.on_event("startup")
async def on_startup():
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook")
