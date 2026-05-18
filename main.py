import os
import logging
import asyncio
import asyncpg
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, types
from aiogram.types import Update

# 1. Ініціалізація логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 2. Ініціалізація FastAPI
app = FastAPI()

# 3. Зчитування конфігурації з Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

# 4. Ініціалізація компонентів Telegram
bot = None
if BOT_TOKEN:
    bot = Bot(token=BOT_TOKEN)
else:
    logger.error("КРИТИЧНО: BOT_TOKEN відсутній у змінних оточення!")

dp = Dispatcher()

# Глобальна змінна для пулу з'єднань
db_pool = None

async def init_db():
    """ Ініціалізація підключення до БД в безпечному режимі """
    global db_pool
    if not DATABASE_URL:
        logger.error("DATABASE_URL не знайдено в змінних оточення!")
        return

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("База даних успішно підключена, пул створено!")
    except Exception as e:
        logger.error(f"Помилка підключення до бази даних: {e}")

@app.on_event("startup")
async def on_startup(app=None):
    """ 
    Старт сервера: додано параметр app=None, щоб уникнути TypeError 
    у різних версіях FastAPI / Uvicorn.
    """
    await init_db()
    
    if bot and BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        try:
            await bot.set_webhook(webhook_url)
            logger.info(f"Вебхук успішно встановлено на: {webhook_url}")
        except Exception as e:
            logger.error(f"Помилка встановлення вебхука Telegram: {e}")
    else:
        logger.error("Не вистачає BASE_URL або BOT_TOKEN для реєстрації вебхука!")

@app.on_event("shutdown")
async def on_shutdown():
    """ Зупинка сервера """
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("Пул підключень до БД закрито.")
    if bot:
        await bot.session.close()

@app.post("/webhook")
async def webhook(update: dict):
    """ Прийом оновлень від Telegram """
    if not bot:
        return {"status": "error", "message": "Bot not initialized"}
    try:
        telegram_update = Update(**update)
        await dp.feed_update(bot, telegram_update)
    except Exception as e:
        logger.error(f"Помилка обробки вебхука: {e}")
    return {"status": "ok"}

@dp.message()
async def echo_handler(message: types.Message):
    """ Простий тест-хендлер """
    await message.answer(f"Бот на зв'язку! Отримано: {message.text}")

@app.get("/")
async def root():
    """ Перевірочна сторінка для Render сайту """
    return {"message": "StopHotobot працює в штатному режимі!"}
