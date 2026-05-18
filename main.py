import os
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import Update

# Налаштування логів
logging.basicConfig(level=logging.INFO)

# Ініціалізація (Токени додамо на сервері в змінні оточення)
TOKEN = os.getenv("BOT_TOKEN")
# Цю адресу ми отримаємо від Render і вставимо в налаштування
BASE_URL = os.getenv("BASE_URL") 

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# Базовий роут для перевірки, що сервер живий
@app.get("/")
async def root():
    return {"status": "working", "message": "Бот активований!"}

# Головний ендпоінт для Telegram Webhook
@app.post("/webhook")
async def telegram_webhook(request: Request):
    logging.info("Отримано апдейт від Telegram")
    json_str = await request.json()
    update = Update.model_validate(json_str, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

# Ендпоінт, який ми потім віддамо Монобанку!
@app.post("/mono-webhook")
async def mono_webhook(request: Request):
    logging.info("Отримано сигнал про оплату від Monobank!")
    payload = await request.json()
    
    # ТУТ пізніше буде логіка обробки оплати
    print("Дані від Моно:", payload)
    
    return {"status": "accepted"}

# Проста команда старт для тесту
@dp.message(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer(f"Привіт, Вова! Я твоя заготовка бота. Скоро тут будуть твої тексти та оплата!")

# Подія при запуску сервера: реєструємо вебхук в Telegram
@app.on_event("startup")
async def on_startup():
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url)
        logging.info(f"Webhook set to {webhook_url}")
