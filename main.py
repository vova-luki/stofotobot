import os
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncpg

# ==========================================
# 1. КОНФІГУРАЦІЯ ТА ЗМІННІ ОТОЧЕННЯ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
BASE_URL = os.getenv("BASE_URL", "https://stophotobot-1.onrender.com")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 124303561
MONOBANK_PAY_URL = "https://send.monobank.ua/jar/example"  # Легко змінюється в коді

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_pool = None

# Стан гри через FSM
class GameStates(StatesGroup):
    rules = State()
    playing = State()
    game_over = State()

# Медіа-файли (ID або прямі URL до зображень)
# Заміни "1.png", "2.png", "3.png" на реальні file_id або URL після завантаження в Telegram
IMG_RULES = "https://raw.githubusercontent.com/vova-luki/stophotobot/main/1.png"
IMG_START = "https://raw.githubusercontent.com/vova-luki/stophotobot/main/2.png"
IMG_END = "https://raw.githubusercontent.com/vova-luki/stophotobot/main/3.png"

# ==========================================
# 2. ПОМОЖНІ ФУНКЦІЇ ДЛЯ БД ТА ЛОГІКИ
# ==========================================
async def init_db():
    """Створення таблиць, якщо вони відсутні в Supabase"""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_pro BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS games (
                chat_id BIGINT PRIMARY KEY,
                current_round INT DEFAULT 1,
                max_rounds INT DEFAULT 10,
                is_pro_mode BOOLEAN DEFAULT FALSE,
                scores JSONB DEFAULT '{}'::jsonb,
                history JSONB DEFAULT '[]'::jsonb
            );
        """)

async def check_pro_status(chat_id: BIGINT = None, user_id: int = None) -> bool:
    """Перевіряє, чи є користувач PRO або чи є PRO-гравці у чаті"""
    async with db_pool.acquire() as conn:
        if user_id:
            row = await conn.fetchrow("SELECT is_pro FROM users WHERE user_id = $1", user_id)
            if row and row['is_pro']:
                return True
        if chat_id:
            # Перевірка, чи є хоча б один PRO-юзер серед тих, хто взаємодіяв
            # Для надійності при додаванні бота перевіряємо через Telegram (якщо є можливість) 
            # або за наявною історією юзерів у базі
            pass
    return False

async def get_chat_member_count_active(chat_id: int) -> int:
    """Отримує реальну кількість людей у групі (без урахування ботів)"""
    try:
        count = await bot.get_chat_member_count(chat_id)
        return count - 1 if count > 1 else 1
    except Exception:
        return 2  # Дефолтний фолбек для стабільності гри

def get_user_display_name(message_or_from: types.User) -> str:
    """Повертає ім'я профілю або @username, якщо імені немає"""
    if message_or_from.first_name:
        return message_or_from.first_name
    return f"@{message_or_from.username}" if message_or_from.username else "Гравець"

# ==========================================
# 3. ШАБЛОНИ ТЕКСТІВ ТА КНОПОК (СТРОГО ЗА ТЗ)
# ==========================================
def get_rules_text():
    return (
        "Вітаємо у грі 100 PHOTO!\n"
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n"
        "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 фото.\n"
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

async def send_start_flow(chat_id: int, is_pro_group: bool = False):
    players_count = await get_chat_member_count_active(chat_id)
    
    if not is_pro_group:
        if players_count == 1:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")]])
            await bot.send_message(chat_id, "Щоб грати, додайте в групу другого гравця.\nЩоб перезапустити бота, напишіть в чат команду /start або /play.\n[ НОВА ГРА ДО 10 ]", reply_markup=kb)
            return
        elif players_count >= 3:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="buy_pro")]])
            await bot.send_message(chat_id, "Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\nPro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах Pro-гравця\n\n[ КУПИТИ PRO-ВЕРСІЮ ]", reply_markup=kb)
            return
    else:
        if players_count == 1:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")]])
            await bot.send_message(chat_id, "Щоб грати, додайте в групу другого гравця.\nЩоб перезапустити бота, напишіть в чат команду /start або /play.\n[ НОВА ГРА ДО 10 ]", reply_markup=kb)
            return
        elif players_count > 10:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="we_are_10")]])
            await bot.send_message(chat_id, "На жаль, грати може максимум 10 гравців.\nЩоб перезапустити бота, напишіть в чат команду /start або /play.\n[ НАС ВЖЕ 10 ]", reply_markup=kb)
            return

    # Валідний запуск гри
    kb_list = []
    if is_pro_group:
        kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")])
    else:
        kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")])
        kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_to_pay")])
        kb_list.append([types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_to_pay")])

    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_list)
    
    # Текст правил гри з гіперпосиланням (ТЗ: назва гри має бути текстом з посиланням)
    rules_caption = get_rules_text().replace("Вітаємо у грі 100 PHOTO!", "Вітаємо у грі <a href='https://t.me/stophotobot'>100 PHOTO</a>!")
    await bot.send_photo(chat_id, photo=IMG_RULES, caption=rules_caption, parse_mode="HTML", reply_markup=markup)

# ==========================================
# 4. ОБРОБНИКИ ДЛЯ ПРИВАТНИХ ЧАТІВ (PRIVACY)
# ==========================================
@dp.message(F.chat.type == "private", Command("stat"))
async def admin_stat(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
        
    async with db_pool.acquire() as conn:
        # Одночасне виконання агрегатних запитів для швидкодії
        now = datetime.utcnow()
        queries = {
            "all_chats": "SELECT COUNT(*) FROM chats",
            "all_users": "SELECT COUNT(*) FROM users",
            "free_users": "SELECT COUNT(*) FROM users WHERE is_pro = FALSE",
            "pro_users": "SELECT COUNT(*) FROM users WHERE is_pro = TRUE",
            
            "chats_24h": "SELECT COUNT(*) FROM chats WHERE created_at >= $1",
            "users_24h": "SELECT COUNT(*) FROM users WHERE created_at >= $1",
            "free_24h": "SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= $1",
            "pro_24h": "SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= $1",

            "chats_7d": "SELECT COUNT(*) FROM chats WHERE created_at >= $1",
            "users_7d": "SELECT COUNT(*) FROM users WHERE created_at >= $1",
            "free_7d": "SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= $1",
            "pro_7d": "SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= $1",

            "chats_30d": "SELECT COUNT(*) FROM chats WHERE created_at >= $1",
            "users_30d": "SELECT COUNT(*) FROM users WHERE created_at >= $1",
            "free_30d": "SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= $1",
            "pro_30d": "SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= $1",

            "chats_1y": "SELECT COUNT(*) FROM chats WHERE created_at >= $1",
            "users_1y": "SELECT COUNT(*) FROM users WHERE created_at >= $1",
            "free_1y": "SELECT COUNT(*) FROM users WHERE is_pro = FALSE AND created_at >= $1",
            "pro_1y": "SELECT COUNT(*) FROM users WHERE is_pro = TRUE AND created_at >= $1",
        }
        
        res = {}
        res["all_chats"] = await conn.fetchval(queries["all_chats"])
        res["all_users"] = await conn.fetchval(queries["all_users"])
        res["free_users"] = await conn.fetchval(queries["free_users"])
        res["pro_users"] = await conn.fetchval(queries["pro_users"])
        
        res["chats_24h"] = await conn.fetchval(queries["chats_24h"], now - timedelta(days=1))
        res["users_24h"] = await conn.fetchval(queries["users_24h"], now - timedelta(days=1))
        res["free_24h"] = await conn.fetchval(queries["free_24h"], now - timedelta(days=1))
        res["pro_24h"] = await conn.fetchval(queries["pro_24h"], now - timedelta(days=1))

        res["chats_7d"] = await conn.fetchval(queries["chats_7d"], now - timedelta(days=7))
        res["users_7d"] = await conn.fetchval(queries["users_7d"], now - timedelta(days=7))
        res["free_7d"] = await conn.fetchval(queries["free_7d"], now - timedelta(days=7))
        res["pro_7d"] = await conn.fetchval(queries["pro_7d"], now - timedelta(days=7))

        res["chats_30d"] = await conn.fetchval(queries["chats_30d"], now - timedelta(days=30))
        res["users_30d"] = await conn.fetchval(queries["users_30d"], now - timedelta(days=30))
        res["free_30d"] = await conn.fetchval(queries["free_30d"], now - timedelta(days=30))
        res["pro_30d"] = await conn.fetchval(queries["pro_30d"], now - timedelta(days=30))

        res["chats_1y"] = await conn.fetchval(queries["chats_1y"], now - timedelta(days=365))
        res["users_1y"] = await conn.fetchval(queries["users_1y"], now - timedelta(days=365))
        res["free_1y"] = await conn.fetchval(queries["free_1y"], now - timedelta(days=365))
        res["pro_1y"] = await conn.fetchval(queries["pro_1y"], now - timedelta(days=365))

    stat_text = (
        f"ЗА ВЕСЬ ЧАС:\n"
        f"- всі чати: {res['all_chats']}\n"
        f"- всі юзери: {res['all_users']}\n"
        f"- free-юзери: {res['free_users']}\n"
        f"- pro-юзери: {res['pro_users']}\n\n"
        f"ПРИРІСТ ЗА РІК:\n"
        f"- всі чати: +{res['chats_1y']}\n"
        f"- всі юзери: +{res['users_1y']}\n"
        f"- free-юзери: +{res['free_1y']}\n"
        f"- pro-юзери: +{res['pro_1y']}\n\n"
        f"ПРИРІСТ ЗА 30 ДНІВ:\n"
        f"- всі чати: +{res['chats_30d']}\n"
        f"- всі юзери: +{res['users_30d']}\n"
        f"- free-юзери: +{res['free_30d']}\n"
        f"- pro-юзери: +{res['pro_30d']}\n\n"
        f"ПРИРІСТ ЗА 7 ДНІВ:\n"
        f"- всі чати: +{res['chats_7d']}\n"
        f"- всі юзери: +{res['users_7d']}\n"
        f"- free-юзери: +{res['free_7d']}\n"
        f"- pro-юзери: +{res['pro_7d']}\n\n"
        f"ПРИРІСТ ЗА 24 ГОД:\n"
        f"- всі чати: +{res['chats_24h']}\n"
        f"- всі юзери: +{res['users_24h']}\n"
        f"- free-юзери: +{res['free_24h']}\n"
        f"- pro-юзери: +{res['pro_24h']}"
    )
    await message.answer(stat_text)

@dp.message(F.chat.type == "private")
async def private_stub(message: types.Message):
    """Заглушка для звичайних користувачів у приватних повідомленнях"""
    if message.from_user.id == ADMIN_ID:
        return
    await message.answer(
        "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). найдеш мене через пошук @stophotobot"
    )

# ==========================================
# 5. ДИНАМІЧНИЙ МОНІТОРИНГ СКЛАДУ ГРУПИ
# ==========================================
@dp.my_chat_member()
async def bot_added_to_chat(event: types.ChatMemberUpdated):
    """Тригер додавання бота в групу"""
    if event.new_chat_member.status in ["member", "administrator"]:
        chat_id = event.chat.id
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO chats (chat_id) VALUES ($1) ON CONFLICT (chat_id) DO NOTHING", chat_id)
            # Реєструємо ініціатора як базового юзера
            await conn.execute("INSERT INTO users (user_id, username, full_name) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO NOTHING", 
                               event.from_user.id, event.from_user.username, event.from_user.full_name)
        
        is_pro = await check_pro_status(chat_id=chat_id, user_id=event.from_user.id)
        await send_start_flow(chat_id, is_pro_group=is_pro)

@dp.message(F.chat.type.in_(["group", "supergroup"]), F.new_chat_members)
async def new_members_handler(message: types.Message):
    """Перевірка лімітів під час додавання нових учасників у процесі гри"""
    chat_id = message.chat.id
    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT is_pro_mode FROM games WHERE chat_id = $1", chat_id)
    
    is_pro_group = game['is_pro_mode'] if game else await check_pro_status(chat_id=chat_id)
    players_count = await get_chat_member_count_active(chat_id)

    if not is_pro_group and players_count >= 3:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="buy_pro")]])
        await message.answer("Щоб грати втрьох і більше, хоча б 1 гравець має бути Pro.\nPro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах Pro-гравця\n\n[ КУПИТИ PRO-ВЕРСІЮ ]", reply_markup=kb)
    elif is_pro_group and players_count > 10:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="we_are_10")]])
        await message.answer("На жаль, грати може максимум 10 гравців.\nЩоб перезапустити бота, напишіть в чат команду /start або /play.\n[ НАС ВЖЕ 10 ]", reply_markup=kb)

# ==========================================
# 6. КОМАНДИ ГРУПОВОЇ ГРИ ТА CALLBACKS
# ==========================================
@dp.message(F.chat.type.in_(["group", "supergroup"]), Command("start", "play"))
async def reset_game_command(message: types.Message, state: FSMContext):
    await state.clear()
    is_pro = await check_pro_status(chat_id=message.chat.id, user_id=message.from_user.id)
    await send_start_flow(message.chat.id, is_pro_group=is_pro)

@dp.callback_query(F.data.in_(["new_game_10", "new_game_pro", "we_are_10"]))
async def init_game_rounds(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    is_pro_mode = callback.data == "new_game_pro"
    max_rounds = 100 if is_pro_mode else 10
    
    # Реєстрація старту в БД
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO games (chat_id, current_round, max_rounds, is_pro_mode, scores, history)
            VALUES ($1, 1, $2, $3, '{}'::jsonb, '[]'::jsonb)
            ON CONFLICT (chat_id) DO UPDATE 
            SET current_round = 1, max_rounds = $2, is_pro_mode = $3, scores = '{}'::jsonb, history = '[]'::jsonb
        """, chat_id, max_rounds, is_pro_mode)

    await callback.answer()
    
    # Текст Завдання 1 за ТЗ
    task_text = (
        "Рахунок\n"
        "player 1: 0\n"
        "player 2: 0\n\n"
        "Завдання: 1\n\n"
        "Знайди і сфотографуй число 1."
    )
    if is_pro_mode:
        task_text = task_text.replace("player 2: 0", "player 2: 0\n…\nplayer N: 0")

    await bot.send_photo(chat_id, photo=IMG_START, caption=task_text)
    await state.set_state(GameStates.playing)

@dp.callback_query(F.data == "go_to_pay")
async def show_payment_post(callback: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=MONOBANK_PAY_URL)],
        [types.InlineKeyboardButton(text="ПРОДОВЖИТИ ГРУ УДВОХ", callback_data="new_game_10")]
    ])
    pay_text = (
        "Pro-версія гри:\n"
        "- до 10 гравців\n"
        "- до 100 раундів назавжди\n"
        "- у всіх чатах Pro-гравця\n\n"
        "[ КУПИТИ PRO-ВЕРСІЮ ]\n"
        "[ ПРОДОВЖИТИ ГРУ УДВОХ ]"
    )
    await bot.send_message(callback.message.chat.id, pay_text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "buy_pro")
async def show_pure_pay_link(callback: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=MONOBANK_PAY_URL)]])
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("rollback_"))
async def rollback_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    _, round_to_cancel_str = callback.data.split("_")
    round_to_cancel = int(round_to_cancel_str)
    
    async with db_pool.acquire() as conn:
        game = await conn.fetchrow("SELECT current_round, max_rounds, is_pro_mode, scores, history FROM games WHERE chat_id = $1", chat_id)
        if not game or game['current_round'] <= 1:
            await callback.answer("Неможливо скасувати!")
            return
            
        import json
        history = json.loads(game['history'])
        scores = json.loads(game['scores'])
        
        if history:
            last_action = history.pop()
            last_player = last_action.get("user_id")
            if last_player in scores and scores[last_player] > 0:
                scores[last_player] -= 1
                
        prev_round = game['current_round'] - 1
        
        await conn.execute(
            "UPDATE games SET current_round = $1, scores = $2, history = $3 WHERE chat_id = $4",
            prev_round, json.dumps(scores), json.dumps(history), chat_id
        )

    await callback.answer("Раунд скасовано!")
    # Формування тексту попереднього раунду
    score_lines = []
    for uid, pts in scores.items():
        score_lines.append(f"{uid}: {pts}")
    score_block = "\n".join(score_lines) if score_lines else "@user1: ...\n@user2: ..."
    
    kb_list = [[types.InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {prev_round-1}", callback_data=f"rollback_{prev_round-1}")]]
    if game['is_pro_mode']:
        kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")])
    else:
        kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")])
        kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_to_pay")])
        kb_list.append([types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_to_pay")])
        
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_list)
    await bot.send_message(chat_id, f"Рахунок\n{score_block}\n\nЗавдання: {prev_round}", reply_markup=markup)

# ==========================================
# 7. МЕХАНІКА ОБРОБКИ ІГРОВИХ ФОТОКАРТОК
# ==========================================
@dp.message(F.chat.type.in_(["group", "supergroup"]), F.photo)
async def game_photo_handler(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = get_user_display_name(message.from_user)
    
    async with db_pool.acquire() as conn:
        # Зберігаємо учасника у загальну базу
        await conn.execute("INSERT INTO users (user_id, username, full_name) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO NOTHING", 
                           user_id, message.from_user.username, message.from_user.full_name)
        
        game = await conn.fetchrow("SELECT current_round, max_rounds, is_pro_mode, scores, history FROM games WHERE chat_id = $1", chat_id)
        if not game:
            return

        import json
        current_round = game['current_round']
        max_rounds = game['max_rounds']
        is_pro = game['is_pro_mode']
        scores = json.loads(game['scores'])
        history = json.loads(game['history'])
        
        # Нарахування балу
        scores[user_name] = scores.get(user_name, 0) + 1
        history.append({"round": current_round, "user_id": user_name, "timestamp": datetime.utcnow().isoformat()})
        
        if current_round >= max_rounds:
            # КІНЕЦЬ ГРИ
            winner = max(scores, key=scores.get) if scores else "@user"
            score_block = "\n".join([f"{k}: {v}" for k, v in scores.items()])
            
            kb_list = [
                [types.InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {max_rounds}", callback_data=f"rollback_{max_rounds}")],
            ]
            if is_pro:
                kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")])
            else:
                kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")])
                kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_to_pay")])
                kb_list.append([types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_to_pay")])
                
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb_list)
            end_text = (
                f"Рахунок\n{score_block}\n\n"
                f"Переможець: {winner}\n\n"
                f"Не забудь про свій приз!"
            )
            await bot.send_photo(chat_id, photo=IMG_END, caption=end_text, reply_markup=markup)
            await state.set_state(GameStates.game_over)
        else:
            # НАСТУПНИЙ РАУНД
            next_round = current_round + 1
            await conn.execute(
                "UPDATE games SET current_round = $1, scores = $2, history = $3 WHERE chat_id = $4",
                next_round, json.dumps(scores), json.dumps(history), chat_id
            )
            
            score_block = "\n".join([f"{k}: {v}" for k, v in scores.items()])
            kb_list = [[types.InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"rollback_{current_round}")]]
            if is_pro:
                kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")])
                if "…" not in score_block:
                    score_block += "\n…\nplayer N: 0"
            else:
                kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="new_game_10")])
                kb_list.append([types.InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="go_to_pay")])
                kb_list.append([types.InlineKeyboardButton(text="ДОДАТИ ГРАВЦІВ", callback_data="go_to_pay")])
                
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb_list)
            task_text = (
                f"Рахунок\n{score_block}\n\n"
                f"Завдання: {next_round}"
            )
            await bot.send_message(chat_id, task_text, reply_markup=markup)

# ==========================================
# 8. LIFESPAN СЕРВЕРА FASTAPI ТА ВЕБХУКИ
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    # Старт: створюємо connection pool до Supabase
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await init_db()
    
    # Встановлюємо Вебхук для Telegram
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    yield
    # Зупинка: закриваємо пул та видаляємо вебхук
    await bot.delete_webhook()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Ендпоінт для отримання оновлень від Telegram API"""
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=status.HTTP_200_OK)

@app.post("/monobank_webhook")
async def monobank_webhook(request: Request):
    """Обробник успішної оплати через еквайринг Monobank"""
    data = await request.json()
    
    # Приклад структури Monobank: data['statementItem']['amount'] (в копійках)
    # Та data['statementItem']['comment'] або custom екстра-поля з Telegram User ID всередині
    statement = data.get("statementItem", {})
    amount = statement.get("amount", 0)
    
    if amount >= 10000:  # 100 грн в копійках
        # Для демонстрації екстрагуємо user_id з коментаря або інвойсу платника
        # У реальному кейсі передавай user_id при створенні лінку або валідуй за надісланими параметрами
        user_id = ADMIN_ID  # Тестове значення для перевірки
        
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_pro = TRUE WHERE user_id = $1", user_id)
            # Отримуємо останній чат користувача для відправки повідомлення
            chat_row = await conn.fetchrow("SELECT chat_id FROM games ORDER BY current_round DESC LIMIT 1")
            
        if chat_row:
            chat_id = chat_row['chat_id']
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="НОВА ГРА", callback_data="new_game_pro")]])
            success_text = (
                "Дякую, оплата є!\n"
                "– @user тепер Pro\n"
                "– відкрито 100 раундів\n"
                "– відкрито 10 гравців\n\n"
                "[ НОВА ГРА ]"
            )
            await bot.send_message(chat_id, success_text, reply_markup=kb)
            
    return Response(status_code=status.HTTP_200_OK)
