import os
import logging
import asyncio
import json
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg

# Логування для відстеження роботи на Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Зчитування змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Критична помилка: Перевірте наявність BOT_TOKEN, BASE_URL та DATABASE_URL у панелі Render.")

ADMIN_ID = 124303561
# URL-адреса для QR-оплати Monobank
MONOBANK_PAY_URL = "https://send.monobank.ua/jar/example" 

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool: Optional[asyncpg.Pool] = None

# --- Робота з базою даних Supabase/PostgreSQL ---
async def init_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL, 
            min_size=1, 
            max_size=10,
            statement_cache_size=0 
        )

async def register_chat(chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chats (chat_id, is_pro, created_at) VALUES ($1, FALSE, NOW()) ON CONFLICT (chat_id) DO NOTHING",
            chat_id
        )

async def check_chat_pro_status(chat_id: int) -> bool:
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT is_pro FROM chats WHERE chat_id = $1", chat_id)
        if val:
            return True
        return False

async def get_game_state(chat_id: int) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT current_round, scores FROM games WHERE chat_id = $1", chat_id)
        if row:
            return {"current_round": row["current_round"], "scores": json.loads(row["scores"])}
        return None

async def save_game_state(chat_id: int, current_round: int, scores: dict):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO games (chat_id, current_round, scores, updated_at) VALUES ($1, $2, $3, NOW()) "
            "ON CONFLICT (chat_id) DO UPDATE SET current_round = $2, scores = $3, updated_at = NOW()",
            chat_id, current_round, json.dumps(scores)
        )

async def get_clean_member_count(chat_id: int) -> int:
    try:
        count = await bot.get_chat_member_count(chat_id)
        return max(1, count - 1)  # Захист: мінімум 1 гравець, якщо бот не зміг порахувати чат
    except TelegramAPIError as e:
        logger.error(f"Помилка підрахунку учасників у чаті {chat_id}: {e}")
        return 2 # Дефолтне значення для безпечного старту

# --- Головна логіка перевірки учасників та відправки постів ---
async def evaluate_and_send_post(chat_id: int, trigger_user_id: Optional[int] = None):
    try:
        await register_chat(chat_id)
        players_count = await get_clean_member_count(chat_id)
        is_pro = await check_chat_pro_status(chat_id)

        # Якщо ліміт безкоштовної версії перевищено
        if not is_pro and players_count >= 3:
            builder = InlineKeyboardBuilder()
            builder.button(text="КУПИТИ PRO-ВЕРСІЮ", url=MONOBANK_PAY_URL)
            await bot.send_message(
                chat_id,
                "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\n\n"
                "Pro-версія гри:\n"
                "- до 10 гравців\n"
                "- до 100 раундів назавжди\n"
                "- у всіх чатах Pro-гравця",
                reply_markup=builder.as_markup()
            )
            return

        # Максимум для PRO системи
        if is_pro and players_count >= 11:
            builder = InlineKeyboardBuilder()
            builder.button(text="НАС ВЖЕ 10", callback_data="refresh_status")
            await bot.send_message(
                chat_id,
                "На жаль, грати може максимум 10 гравців.\n\n"
                "Щоб перезапустити бота, напишіть в чат команду /start або /play.",
                reply_markup=builder.as_markup()
            )
            return

        # Якщо гравець один
        if players_count <= 1:
            builder = InlineKeyboardBuilder()
            builder.button(text="НОВА ГРА ДО 10" if not is_pro else "НОВА ГРА", callback_data="new_game_10" if not is_pro else "new_game_100")
            await bot.send_message(
                chat_id,
                "Щоб грати, додайте в групу другого гравця.\n\n"
                "Щоб перезапустити бота, напишіть в чат команду /start або /play.",
                reply_markup=builder.as_markup()
            )
            return

        # Валідний запуск — надсилаємо правила
        builder = InlineKeyboardBuilder()
        if not is_pro:
            builder.button(text="НОВА ГРА ДО 10", callback_data="new_game_10")
            builder.button(text="НОВА ГРА ДО 100 (PRO)", callback_data="trigger_payment")
            builder.button(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="trigger_payment")
            builder.adjust(1)
        else:
            builder.button(text="НОВА ГРА", callback_data="new_game_100")
            
        rules_text = (
            "Вітаємо у грі <a href=\"https://t.me/stophotobot\">100 PHOTO</a>!\n"
            "Правила гри:\n\n"
            "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n"
            "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 photo.\n"
            "За кожне photo гравець отримує 1 бал.\n\n"
            "3. Числа не можна створювати (викладати предметами) або писати самому.\n"
            "Лише photoграфувати їх вдома, на вулиці тощо.\n\n"
            "4. Не можна брати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо).\n"
            "Локації мають бути різними.\n\n"
            "5. Якщо надіслане foto не відповідає правилам, це foto можна відмінити і почати раунд заново.\n"
            "Щоб перезапустити бота, напишіть у чат команду /start або /play.\n\n"
            "За бажанням, придумайте приз переможцю.\n\n"
            "Натхнення!"
        )
        await bot.send_message(chat_id, rules_text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)
    except TelegramAPIError as e:
        logger.error(f"Помилка відправки повідомлення у чат {chat_id}: {e}")

# --- Обробники для Групових чатів ---

@dp.my_chat_member(
    F.old_chat_member.status.in_({"left", "kicked"}) & 
    F.new_chat_member.status.in_({"member", "administrator"})
)
async def bot_added_to_group(event: ChatMemberUpdated):
    if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await evaluate_and_send_post(event.chat.id, trigger_user_id=event.from_user.id)

@dp.chat_member(F.new_chat_member.status == "member")
async def user_joined_group(event: ChatMemberUpdated):
    if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        logger.info(f"Користувач {event.new_chat_member.user.id} зайшов у групу {event.chat.id}")

@dp.chat_member(F.new_chat_member.status.in_({"left", "kicked"}))
async def user_left_group(event: ChatMemberUpdated):
    if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        logger.info(f"Користувач {event.old_chat_member.user.id} покинув групу {event.chat.id}")

@dp.message(F.chat.type.in_({"group", "supergroup"}), Command(commands=["start", "play"]))
async def group_reset_command(message: types.Message):
    await evaluate_and_send_post(message.chat.id, trigger_user_id=message.from_user.id)

@dp.callback_query(F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.data == "refresh_status")
async def refresh_callback(callback: types.CallbackQuery):
    await evaluate_and_send_post(callback.message.chat.id, trigger_user_id=callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.data == "trigger_payment")
async def payment_post_callback(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="КУПИТИ PRO-ВЕРСІЮ", url=MONOBANK_PAY_URL)
    builder.button(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="new_game_10")
    builder.adjust(1)
    
    pay_text = (
        "Pro-версія гри:\n"
        "- до 10 гравців\n"
        "- до 100 раундів назавжди\n"
        "- у всіх чатах Pro-гравця"
    )
    await callback.message.answer(pay_text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.data.in_({"new_game_10", "new_game_100"}))
async def init_game_rounds(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await save_game_state(chat_id, 1, {})
    
    start_text = (
        "Рахунок\n"
        "player 1: 0\n"
        "player 2: 0\n\n"
        "Завдання: 1\n\n"
        "Знайди і сфотографуй число 1."
    )
    if callback.data == "new_game_100":
        start_text = (
            "Рахунок\n"
            "player 1: 0\n"
            "player 2: 0\n"
            "…\n"
            "player N: 0\n\n"
            "Завдання: 1\n\n"
            "Знайди і сфотографуй число 1."
        )
    await callback.message.answer(start_text)
    await callback.answer()

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.photo)
async def process_incoming_photo(message: types.Message):
    chat_id = message.chat.id
    game = await get_game_state(chat_id)
    if not game or game["current_round"] == 0:
        return

    is_pro = await check_chat_pro_status(chat_id)
    max_rounds = 100 if is_pro else 10
    max_players = 10 if is_pro else 2
    
    players_count = await get_clean_member_count(chat_id)
    if players_count > max_players:
        await evaluate_and_send_post(chat_id)
        return

    current_round = game["current_round"]
    scores = game["scores"]
    
    user_identity = message.from_user.full_name if message.from_user.full_name else f"@{message.from_user.username}"
    scores[user_identity] = scores.get(user_identity, 0) + 1
    
    # Динамічне формування списку гравців із заглушками
    score_board = ["Рахунок"]
    for usr, pts in scores.items():
        score_board.append(f"{usr}: {pts}")
    
    # Додаємо автоматичні заглушки до ліміту реальних учасників групи (мінімум до 2)
    total_slots = max(2, players_count)
    for i in range(len(scores), total_slots):
        score_board.append(f"player {i + 1}: 0")
        
    score_text = "\n".join(score_board)

    if current_round >= max_rounds:
        winner = max(scores, key=scores.get) if scores else "@user"
        end_text = f"{score_text}\n\nПереможець: {winner}\n\nНе забудь про свій приз!"
        
        builder = InlineKeyboardBuilder()
        builder.button(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"undo_{current_round}")
        if is_pro:
            builder.button(text="НОВА ГРА", callback_data="new_game_100")
        else:
            builder.button(text="НОВА ГРА ДО 10", callback_data="new_game_10")
            builder.button(text="НОВА ГРА ДО 100 (PRO)", callback_data="trigger_payment")
            builder.button(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="trigger_payment")
        builder.adjust(1)
        
        await save_game_state(chat_id, 0, scores)
        await message.answer(end_text, reply_markup=builder.as_markup())
    else:
        next_round = current_round + 1
        await save_game_state(chat_id, next_round, scores)
        
        task_text = f"{score_text}\n\nЗавдання: {next_round}\n\nЗнайди і сфотографуй число {next_round}."
        builder = InlineKeyboardBuilder()
        builder.button(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"undo_{current_round}")
        builder.button(text="НОВА ГРА" if is_pro else "НОВА ГРА ДО 10", callback_data="new_game_100" if is_pro else "new_game_10")
        builder.adjust(1)
        await message.answer(task_text, reply_markup=builder.as_markup())

@dp.callback_query(F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.data.startswith("undo_"))
async def undo_round_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    target_round = int(callback.data.split("_")[1])
    game = await get_game_state(chat_id)
    
    if not game or game["current_round"] == 0:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT scores FROM games WHERE chat_id = $1", chat_id)
            if row:
                game = {"current_round": target_round + 1, "scores": json.loads(row["scores"])}

    if not game:
        await callback.answer("Сесію гри не знайдено.")
        return

    scores = game["scores"]
    user_identity = callback.from_user.full_name if callback.from_user.full_name else f"@{callback.from_user.username}"
    
    if user_identity in scores and scores[user_identity] > 0:
        scores[user_identity] -= 1
        
    await save_game_state(chat_id, target_round, scores)
    is_pro = await check_chat_pro_status(chat_id)
    
    score_board = ["Рахунок"]
    for usr, pts in scores.items():
        score_board.append(f"{usr}: {pts}")
    score_text = "\n".join(score_board)
    
    task_text = f"{score_text}\n\nЗавдання: {target_round}\n\nЗнайди і сфотографуй число {target_round}."
    builder = InlineKeyboardBuilder()
    if target_round > 1:
        builder.button(text=f"ОБНУЛИТИ РАУНД {target_round-1}", callback_data=f"undo_{target_round-1}")
    builder.button(text="НОВА ГРА" if is_pro else "НОВА ГРА ДО 10", callback_data="new_game_100" if is_pro else "new_game_10")
    builder.adjust(1)
    
    await callback.message.answer(task_text, reply_markup=builder.as_markup())
    await callback.answer()

# --- Логіка приватного чату ---

@dp.message(F.chat.type == ChatType.PRIVATE, Command("stat"))
async def sys_admin_stat(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
        
    async with db_pool.acquire() as conn:
        all_chats = await conn.fetchval("SELECT COUNT(*) FROM chats")
        all_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        pro_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE")
        free_users = all_users - pro_users
        
        async def fetch_delta(interval: str):
            c = await conn.fetchval(f"SELECT COUNT(*) FROM chats WHERE created_at >= NOW() - INTERVAL '{interval}'")
            u = await conn.fetchval(f"SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '{interval}'")
            p = await conn.fetchval(f"SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= NOW() - INTERVAL '{interval}'")
            return c, u, p

        c_24h, u_24h, p_24h = await fetch_delta("24 hours")
        c_7d, u_7d, p_7d = await fetch_delta("7 days")
        c_30d, u_30d, p_30d = await fetch_delta("30 days")
        c_1y, u_1y, p_1y = await fetch_delta("1 year")

    report = (
        f"ЗА ВЕСЬ ЧАС:\n"
        f"- всі чати: {all_chats}\n"
        f"- всі юзери: {all_users}\n"
        f"- free-юзери: {free_users}\n"
        f"- pro-юзери: {pro_users}\n\n"
        f"ПРИРІСТ ЗА РІК:\n"
        f"- всі чати: +{c_1y}\n"
        f"- всі юзери: +{u_1y}\n"
        f"- free-юзери: +{u_1y - p_1y}\n"
        f"- pro-юзери: +{p_1y}\n\n"
        f"ПРИРІСТ ЗА 30 ДНІВ:\n"
        f"- всі чати: +{c_30d}\n"
        f"- всі юзери: +{u_30d}\n"
        f"- free-юзери: +{u_30d - p_30d}\n"
        f"- pro-юзери: +{p_30d}\n\n"
        f"ПРИРІСТ ЗА 7 ДНІВ:\n"
        f"- всі чати: +{c_7d}\n"
        f"- всі юзери: +{u_7d}\n"
        f"- free-юзери: +{u_7d - p_7d}\n"
        f"- pro-юзери: +{p_7d}\n\n"
        f"ПРИРІСТ ЗА 24 ГОД:\n"
        f"- всі чати: +{c_24h}\n"
        f"- всі юзери: +{u_24h}\n"
        f"- free-юзери: +{u_24h - p_24h}\n"
        f"- pro-юзери: +{p_24h}"
    )
    await message.answer(report)

@dp.message(F.chat.type == ChatType.PRIVATE)
async def private_stub_response(message: types.Message):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, is_pro, created_at) VALUES ($1, $2, FALSE, NOW()) ON CONFLICT (user_id) DO UPDATE SET username = $2",
            message.from_user.id, message.from_user.username or ""
        )
    if message.from_user.id == ADMIN_ID:
        return
        
    await message.answer(
        "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу).\n\n"
        "Знайдеш мене через пошук – @stophotobot"
    )

# --- FastAPI Webhook Lifespan ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db_pool()
    web_url = f"{BASE_URL}/webhook"
    try:
        await bot.set_webhook(
            url=web_url, 
            drop_pending_updates=True, 
            allowed_updates=["message", "chat_member", "my_chat_member", "callback_query"]
        )
        logger.info(f"Вебхук підключено до: {web_url}")
    except Exception as e:
        logger.error(f"Помилка встановлення вебхуку: {e}")
    yield
    try:
        await bot.delete_webhook()
    except:
        pass
    if db_pool:
        await db_pool.close()

app = FastAPI(lifespan=lifespan)

# --- НОВИЙ МАРШРУТ ДЛЯ ПЕРЕВІРКИ З БОКУ RENDER ---
@app.get("/")
async def root():
    return {"status": "ok", "message": "Bot is running"}

@app.post("/webhook")
async def inbound_tg_updates(request: Request):
    try:
        payload = await request.json()
        update = types.Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Помилка обробки вебхуку: {e}")
        return Response(status_code=status.HTTP_200_OK)
        
# --- Обробка платежів Monobank ---

@app.post("/monobank-webhook")
async def monobank_payment_receiver(request: Request):
    try:
        data = await request.json()
        if data.get("amount", 0) >= 10000:
            custom = data.get("custom_data", {})
            tg_user_id = int(custom.get("user_id", 0))
            
            if tg_user_id:
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET is_pro = TRUE WHERE user_id = $1", tg_user_id)
                
                try:
                    builder = InlineKeyboardBuilder()
                    builder.button(text="НОВА ГРА", callback_data="new_game_100")
                    await bot.send_message(
                        tg_user_id,
                        "Дякую, оплата є!\n"
                        "– @user тепер Pro\n"
                        "– відкрито 100 раундів\n"
                        "– відкрито 10 гравців",
                        reply_markup=builder.as_markup()
                    )
                except TelegramAPIError as tg_err:
                    logger.error(f"Не вдалося надіслати підтвердження у приват {tg_user_id}: {tg_err}")
                    
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Помилка Monobank еквайрингу: {e}")
        return Response(status_code=status.HTTP_200_OK)
