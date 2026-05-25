import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request, Response, status

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatType
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.exceptions import TelegramAPIError

# Налаштування логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Читання конфігурації (без технічного хардкоду за замовчуванням)
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Критична помилка: Відсутні необхідні змінні оточення (BOT_TOKEN, BASE_URL, DATABASE_URL) в панелі Render!")

ADMIN_ID = 124303561
MONOBANK_URL = "https://send.monobank.ua/jar/example"  # Лінк на оплату від 100 грн

# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальний пул підключень до БД
db_pool = None

# --- ТЕКСТОВІ КОНСТАНТИ ВІДПОВІДНО ДО ТЗ ---
TXT_ZAGLUSHKA = "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). Знайдеш мене по пошуку @stofotobot"

TXT_1_PERSON = (
    "Щоб грати, додайте в групу другого гравця.\n\n"
    "Щоб перезапустити бота, напишіть в чат команду /start або /play."
)

TXT_3_PEOPLE = (
    "Щоб грати втрьох і більше, хоч 1 гравець має бути pro.\n\n"
    "Pro-версія гри:\n"
    "- до 10 гравців\n"
    "- до 100 раундів назавжди\n"
    "- у всіх чатах pro-гравця"
)

TXT_11_PEOPLE = (
    "Грати може максимум 10 людей.\n\n"
    "Щоб перезапустити бота, напишіть в чат команду /start або /play."
)

TXT_RULES_FREE = (
    "Правила гри: надсилайте в чат фотографії цифр, які відповідають поточному раунду.\n\n"
    "Гра йде до 10 раундів. Максимум 2 гравців."
)

TXT_RULES_PRO = (
    "Правила PRO-версії: надсилайте в чат фотографії цифр.\n\n"
    "Гра йде до 100 раундів. Дозволено до 10 гравців!"
)

TXT_PAYMENT = (
    "Для активації PRO-версії (гра до 100 раундів, до 10 людей) оплатіть внесок від 100 грн.\n\n"
    "Після оплати гра активується автоматично."
)

TXT_PRO_SUCCESS = (
    "ОПЛАТА УСПІШНА\n\n"
    "PRO-статус активовано назавжди! Тепер вам доступні ігри до 100 раундів та до 10 учасників у групах."
)


# --- ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ БД ---
async def get_db_conn():
    return await db_pool.acquire()

async def release_db_conn(conn):
    await db_pool.release(conn)

async def check_chat_pro_status(chat_id: int) -> bool:
    """Перевіряє, чи є в чаті хоча б один PRO-користувач або сам чат активований як PRO"""
    conn = await get_db_conn()
    try:
        # Перевірка через збережений тип сесії
        row = await conn.fetchrow("SELECT game_type FROM game_sessions WHERE chat_id = $1", chat_id)
        if row and row['game_type'] == 'pro':
            return True
        return False
    finally:
        await release_db_conn(conn)

async def is_user_pro(user_id: int) -> bool:
    conn = await get_db_conn()
    try:
        row = await conn.fetchrow("SELECT is_pro FROM pro_users WHERE user_id = $1", user_id)
        return row['is_pro'] if row else False
    finally:
        await release_db_conn(conn)

async def set_user_pro_status(user_id: int, status_pro: bool):
    conn = await get_db_conn()
    try:
        await conn.execute(
            "INSERT INTO pro_users (user_id, is_pro) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET is_pro = $2, updated_at = NOW()",
            user_id, status_pro
        )
    finally:
        await release_db_conn(conn)

async def load_or_create_session(chat_id: int, default_type='free') -> dict:
    conn = await get_db_conn()
    try:
        row = await conn.fetchrow("SELECT game_type, current_round, players::text, last_photo_user_id FROM game_sessions WHERE chat_id = $1", chat_id)
        if not row:
            await conn.execute(
                "INSERT INTO game_sessions (chat_id, game_type, current_round, players) VALUES ($1, $2, 0, '{}'::jsonb)",
                chat_id, default_type
            )
            return {"game_type": default_type, "current_round": 0, "players": {}, "last_photo_user_id": None}
        return {
            "game_type": row['game_type'],
            "current_round": row['current_round'],
            "players": json.loads(row['players']),
            "last_photo_user_id": row['last_photo_user_id']
        }
    finally:
        await release_db_conn(conn)

async def save_session(chat_id: int, session: dict):
    conn = await get_db_conn()
    try:
        await conn.execute(
            "UPDATE game_sessions SET game_type = $2, current_round = $3, players = $4::jsonb, last_photo_user_id = $5, updated_at = NOW() WHERE chat_id = $1",
            chat_id, session['game_type'], session['current_round'], json.dumps(session['players']), session['last_photo_user_id']
        )
    finally:
        await release_db_conn(conn)


# --- КРИТИЧНИЙ ЗАХИСТ ВІД ПАДІННЯ (БЕЗПЕЧНА ВІДПРАВКА) ---
async def safe_send_message(chat_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as e:
        logger.error(f"Помилка відправки повідомлення в чат {chat_id}: {e}")
        return False


# --- МЕХАНІЗМ ВАЛІДАЦІЇ КІЛЬКОСТІ УЧАСНИКІВ ---
async def validate_group_and_send_post(chat_id: int, session: dict) -> bool:
    """
    Повертає True, якщо група валідна і можна продовжувати. 
    Повертає False, якщо ліміти порушено і надіслано сервісний пост.
    """
    try:
        count = await bot.get_chat_member_count(chat_id)
        players_count = count - 1  # Мінус сам бот
    except TelegramAPIError:
        players_count = len(session.get('players', {})) if session.get('players') else 2

    # Перевірка наявності PRO в чаті
    has_pro = session['game_type'] == 'pro'

    if not has_pro:
        if players_count <= 1:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")]])
            await safe_send_message(chat_id, TXT_1_PERSON, kb)
            return False
        elif players_count >= 3:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=MONOBANK_URL)]])
            await safe_send_message(chat_id, TXT_3_PEOPLE, kb)
            return False
    else:
        if players_count <= 1:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")]])
            await safe_send_message(chat_id, TXT_1_PERSON, kb)
            return False
        elif players_count >= 11:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="refresh_group")]])
            await safe_send_message(chat_id, TXT_11_PEOPLE, kb)
            return False
    return True


def get_user_display_name(user: types.User) -> str:
    if user.first_name:
        return user.first_name
    return f"@{user.username}" if user.username else f"player_{user.id}"


def render_scoreboard(session: dict) -> str:
    players = session.get('players', {})
    if not players:
        return "player 1: 0\nplayer 2: 0"
    
    lines = []
    for p_id, data in players.items():
        lines.append(f"{data['name']}: {data['score']}")
    return "\n".join(lines)


# --- ОБРОБКА ДЛЯ ПРИВАТНИХ ЧАТІВ (ЗАГЛУШКА ТА АДМІН) ---
@dp.message(F.chat.type == ChatType.PRIVATE)
async def private_message_handler(message: types.Message):
    user_id = message.from_user.id
    
    # Логіка Адміністратора
    if user_id == ADMIN_ID:
        if message.text == "/pro":
            await set_user_pro_status(user_id, True)
            await message.answer("Твій статус Pro")
            return
        elif message.text == "/free":
            await set_user_pro_status(user_id, False)
            await message.answer("Твій статус free")
            return
        elif message.text == "/stat":
            # Збір агрегованої статистики (за виключенням тестів адміна)
            conn = await db_pool.acquire()
            try:
                now = datetime.now()
                day_ago = now - timedelta(days=1)
                week_ago = now - timedelta(days=7)
                month_ago = now - timedelta(days=30)
                year_ago = now - timedelta(days=365)

                # За весь час
                chats_all = await conn.fetchval("SELECT COUNT(*) FROM game_sessions")
                g10_all = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round <= 10")
                g100_all = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round > 10")
                users_all = await conn.fetchval("SELECT COUNT(DISTINCT user_id) FROM pro_users WHERE user_id != $1", ADMIN_ID)
                pro_all = await conn.fetchval("SELECT COUNT(*) FROM pro_users WHERE is_pro = true AND user_id != $1", ADMIN_ID)
                free_all = users_all - pro_all

                # За 24 години
                chats_24h = await conn.fetchval("SELECT COUNT(*) FROM game_sessions WHERE updated_at >= $1", day_ago)
                g10_24h = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round <= 10 AND created_at >= $1", day_ago)
                g100_24h = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round > 10 AND created_at >= $1", day_ago)

                # За 7 днів
                chats_7d = await conn.fetchval("SELECT COUNT(*) FROM game_sessions WHERE updated_at >= $1", week_ago)
                g10_7d = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round <= 10 AND created_at >= $1", week_ago)
                g100_7d = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round > 10 AND created_at >= $1", week_ago)

                # За 30 днів
                chats_30d = await conn.fetchval("SELECT COUNT(*) FROM game_sessions WHERE updated_at >= $1", month_ago)
                g10_30d = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round <= 10 AND created_at >= $1", month_ago)
                g100_30d = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round > 10 AND created_at >= $1", month_ago)

                # За рік
                chats_1y = await conn.fetchval("SELECT COUNT(*) FROM game_sessions WHERE updated_at >= $1", year_ago)
                g10_1y = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round <= 10 AND created_at >= $1", year_ago)
                g100_1y = await conn.fetchval("SELECT COUNT(*) FROM game_history WHERE max_round > 10 AND created_at >= $1", year_ago)

                stat_text = (
                    "ЗА ВЕСЬ ЧАС:\n"
                    f"- всі чати: {chats_all}\n"
                    f"- всі ігри до 10: {g10_all}\n"
                    f"- всі ігри до 100: {g100_all}\n"
                    f"- всі юзери: {users_all}\n"
                    f"- free-юзери: {free_all}\n"
                    f"- pro-юзери: {pro_all}\n\n"
                    "ЗА 24 ГОДИНИ:\n"
                    f"- чати: {chats_24h}\n"
                    f"- ігри до 10: {g10_24h}\n"
                    f"- ігри до 100: {g100_24h}\n\n"
                    "ЗА 7 ДНІВ:\n"
                    f"- чати: {chats_7d}\n"
                    f"- ігри до 10: {g10_7d}\n"
                    f"- ігри до 100: {g100_7d}\n\n"
                    "ЗА 30 ДНІВ:\n"
                    f"- чати: {chats_30d}\n"
                    f"- ігри до 10: {g10_30d}\n"
                    f"- ігри до 100: {g100_30d}\n\n"
                    "ЗА РІК:\n"
                    f"- чати: {chats_1y}\n"
                    f"- ігри до 10: {g10_1y}\n"
                    f"- ігри до 100: {g100_1y}"
                )
                await message.answer(stat_text)
                return
            finally:
                await db_pool.release(conn)

    # Заглушка для звичайних користувачів
    await message.answer(TXT_ZAGLUSHKA)


# --- ФОНОВИЙ ПЕРЕХОПЛЕННЯ НІКНЕЙМІВ ТА ХЕНДЛЕР ФОТО У ГРУПАХ ---
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message_handler(message: types.Message):
    chat_id = message.chat.id
    user = message.from_user
    
    # Перевірка на команди перезапуску
    if message.text in ["/start", "/play"]:
        # Визначення стартового типу гри на основі статусу ініціатора
        is_pro = await is_user_pro(user.id)
        g_type = "pro" if is_pro else "free"
        
        session = {"game_type": g_type, "current_round": 0, "players": {}, "last_photo_user_id": None}
        # Автоматичний перший запис ініціатора у відомі гравці
        session['players'][str(user.id)] = {"name": get_user_display_name(user), "score": 0}
        
        await save_session(chat_id, session)
        
        if not await validate_group_and_send_post(chat_id, session):
            return

        if g_type == "free":
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game")],
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_pay")],
                [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_pay")]
            ])
            await safe_send_message(chat_id, TXT_RULES_FREE, kb)
        else:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game")]
            ])
            await safe_send_message(chat_id, TXT_RULES_PRO, kb)
        return

    # Завантаження поточної сесії чату
    session = await load_or_create_session(chat_id)
    
    # Оновлення імені користувача в базі при будь-якій активності (Фоновий перехоплення)
    u_id_str = str(user.id)
    if u_id_str in session['players']:
        session['players'][u_id_str]['name'] = get_user_display_name(user)
    else:
        # Якщо гра ще не розпочалась, лімітуємо фонове наповнення відповідно до типу гри
        max_slots = 10 if session['game_type'] == 'pro' else 2
        if len(session['players']) < max_slots:
            session['players'][u_id_str] = {"name": get_user_display_name(user), "score": 0}
    
    await save_session(chat_id, session)

    # Обробка ходу гри, якщо надіслано фото і раунд активний
    if message.photo and session['current_round'] > 0:
        if not await validate_group_and_send_post(chat_id, session):
            return
        
        current = session['current_round']
        max_rounds = 100 if session['game_type'] == 'pro' else 10
        
        # Нарахування балу
        if u_id_str not in session['players']:
            session['players'][u_id_str] = {"name": get_user_display_name(user), "score": 0}
            
        session['players'][u_id_str]['score'] += 1
        session['last_photo_user_id'] = user.id
        
        if current < max_rounds:
            # Наступний раунд
            next_round = current + 1
            session['current_round'] = next_round
            await save_session(chat_id, session)
            
            scores_rendered = render_scoreboard(session)
            round_text = f"Раунд {next_round}.\n\nРахунок\n{scores_rendered}\n\nЗавдання: число {next_round}"
            
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {next_round-1}", callback_data=f"undo_{next_round-1}")],
                [types.InlineKeyboardButton(text="НОВА ГРА" if session['game_type']=='pro' else "НОВА ГРА ДО 10", callback_data="reset_to_rules")]
            ])
            await safe_send_message(chat_id, round_text, kb)
        else:
            # Кінець гри
            session['current_round'] = 0
            await save_session(chat_id, session)
            
            # Запис в історію для статистики адміна
            conn = await db_pool.acquire()
            try:
                await conn.execute("INSERT INTO game_history (chat_id, game_type, max_round) VALUES ($1, $2, $3)", chat_id, session['game_type'], max_rounds)
            finally:
                await db_pool.release(conn)
                
            scores_rendered = render_scoreboard(session)
            end_text = f"Переможець: {get_user_display_name(user)}\n\nРахунок\n{scores_rendered}\n\nНе забудь про свій приз!"
            
            if session['game_type'] == 'free':
                kb = types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="ОБНУЛИТИ РАУНД 10", callback_data="undo_10")],
                    [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="reset_to_rules")],
                    [types.InlineKeyboardButton(text="НОВА ГРА ДО 100 (PRO)", callback_data="go_pay")],
                    [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ (PRO)", callback_data="go_pay")]
                ])
            else:
                kb = types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="ОБНУЛИТИ РАУНД 100", callback_data="undo_100")],
                    [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="reset_to_rules")]
                ])
            await safe_send_message(chat_id, end_text, kb)


# --- ОБРОБКА ВЗАЄМОДІЇ З КНОПКАМИ (CALLBACK QUERIES) ---
@dp.callback_query()
async def callback_handler(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user = callback.from_user
    data = callback.data
    
    session = await load_or_create_session(chat_id)
    
    # Ізоляція: ігнорування кнопок у приваті для звичайних юзерів
    if callback.message.chat.type == ChatType.PRIVATE and user.id != ADMIN_ID:
        await callback.answer()
        return

    if data in ["new_game_10", "new_game_pro", "reset_to_rules", "refresh_group"]:
        # Повне скидання або перезапуск до стану ПРАВИЛА зі збереженням наявних гравців
        if data == "new_game_10": session['game_type'] = "free"
        if data == "new_game_pro": session['game_type'] = "pro"
        
        session['current_round'] = 0
        session['last_photo_user_id'] = None
        # Скидання балів наявних відомих гравців чату в 0 (КРИТИЧНО - структура не видаляється)
        for p_id in session['players']:
            session['players'][p_id]['score'] = 0
            
        await save_session(chat_id, session)
        
        if not await validate_group_and_send_post(chat_id, session):
            await callback.answer()
            return
            
        if session['game_type'] == "free":
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game")],
                [types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_pay")],
                [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_pay")]
            ])
            await callback.message.answer(TXT_RULES_FREE, reply_markup=kb)
        else:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game")]
            ])
            await callback.message.answer(TXT_RULES_PRO, reply_markup=kb)
            
    elif data == "start_game":
        # Миттєва ідентифікація того, хто натиснув кнопку Старту
        u_id_str = str(user.id)
        if u_id_str not in session['players']:
            session['players'][u_id_str] = {"name": get_user_display_name(user), "score": 0}
        else:
            session['players'][u_id_str]['score'] = 0
            
        session['current_round'] = 1
        await save_session(chat_id, session)
        
        if not await validate_group_and_send_post(chat_id, session):
            await callback.answer()
            return
            
        scores_rendered = render_scoreboard(session)
        # Раунд 1. Містить повний навчальний текст завдання без кнопок
        round_1_text = f"Раунд 1.\n\nРахунок\n{scores_rendered}\n\nЗавдання: сфотографуй число 1."
        await callback.message.answer(round_1_text)
        
    elif data == "go_pay":
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=MONOBANK_URL)],
            [types.InlineKeyboardButton(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="new_game_10")]
        ])
        await callback.message.answer(TXT_PAYMENT, reply_markup=kb)
        
    elif data.startswith("undo_"):
        target_round = int(data.split("_")[1])
        
        # Крок назад: зняття балу з автора останнього фото
        if session['last_photo_user_id']:
            last_uid = str(session['last_photo_user_id'])
            if last_uid in session['players'] and session['players'][last_uid]['score'] > 0:
                session['players'][last_uid]['score'] -= 1
        
        session['current_round'] = target_round
        await save_session(chat_id, session)
        
        scores_rendered = render_scoreboard(session)
        
        if target_round == 1:
            # Повернення на Раунд 1 (без кнопок відміни)
            round_text = f"Раунд 1.\n\nРахунок\n{scores_rendered}\n\nЗавдання: сфотографуй число 1."
            await callback.message.answer(round_text)
        else:
            round_text = f"Раунд {target_round}.\n\nРахунок\n{scores_rendered}\n\nЗавдання: число {target_round}"
            # Після скасування раунду, під ним немає кнопки "Обнулити раунд знову"
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА" if session['game_type']=='pro' else "НОВА ГРА ДО 10", callback_data="reset_to_rules")]
            ])
            await callback.message.answer(round_text, reply_markup=kb)

    await callback.answer()


# --- ПОДІЯ ДОДАВАННЯ БОТА В ГРУПУ (СУЧАСНИЙ ФІЛЬТР АIOGRAM 3) ---
@dp.my_chat_member(ChatMemberUpdatedFilter(member_change=JOIN_TRANSITION))
async def bot_added_to_group(event: types.ChatMemberUpdated):
    chat_id = event.chat.id
    user = event.from_user # Хто додав бота
    
    is_pro = await is_user_pro(user.id)
    g_type = "pro" if is_pro else "free"
    
    session = {"game_type": g_type, "current_round": 0, "players": {}, "last_photo_user_id": None}
    session['players'][str(user.id)] = {"name": get_user_display_name(user), "score": 0}
    await save_session(chat_id, session)
    
    if not await validate_group_and_send_post(chat_id, session):
        return

    if g_type == "free":
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_game")],
            [types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_pay")],
            [types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_pay")]
        ])
        await safe_send_message(chat_id, TXT_RULES_FREE, kb)
    else:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="start_game")]
        ])
        await safe_send_message(chat_id, TXT_RULES_PRO, kb)


# --- ВЕБХУК ДЛЯ ОБРОБКИ ПЛАТЕЖІВ ВІД MONOBANK ---
async def process_monobank_payment(data: dict):
    """
    Приклад структури вебхуку Monobank:
    {
        "type": "StatementItem",
        "data": {
            "account": "...",
            "statementItem": {
                "amount": 10000, -- Сума в копійках (10000 = 100 грн)
                "comment": "PRO_124303561" -- Переданий ID користувача в коментарі
            }
        }
    }
    """
    try:
        item = data.get("data", {}).get("statementItem", {})
        amount = item.get("amount", 0) / 100  # Перевід у гривні
        comment = item.get("comment", "")
        
        if amount >= 100 and comment.startswith("PRO_"):
            user_id = int(comment.split("_")[1])
            await set_user_pro_status(user_id, True)
            
            # Сповіщення користувача
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="НОВА ГРА", callback_data="reset_to_rules")]
            ])
            await safe_send_message(user_id, TXT_PRO_SUCCESS, kb)
    except Exception as e:
        logger.error(f"Помилка обробки вебхуку Monobank: {e}")


# --- FASTAPI АРХІТЕКТУРА ТА LIFESPAN МЕНЕДЖЕР ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    # Ініціалізація пулу підключень через порт 6543
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)
    
    # Реєстрація Вебхука в Telegram
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Вебхук успішно встановлено на: {webhook_url}")
    
    yield
    
    # Очищення при зупинці
    await bot.delete_webhook()
    await db_pool.close()
    logger.info("Вебхук видалено, пул БД закрито.")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root_health_check():
    """Кореневий маршрут для успішного проходження Health Check на Render"""
    return {"status": "healthy", "bot": "100 ФОТО"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Ендпоінт для отримання апдейтів від Telegram Bot API"""
    try:
        update_data = await request.json()
        update = types.Update.model_validate(update_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Помилка обробки апдейту Telegram: {e}")
    return Response(status_code=status.HTTP_200_OK)

@app.post("/monobank-webhook")
async def monobank_webhook(request: Request):
    """Ендпоінт для отримання вебхуків оплати від Monobank"""
    try:
        payment_data = await request.json()
        await process_monobank_payment(payment_data)
    except Exception as e:
        logger.error(f"Помилка на ендпоінті монобанку: {e}")
    return Response(status_code=status.HTTP_200_OK)
