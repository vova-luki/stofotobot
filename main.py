import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, ChatMemberUpdatedFilter
from aiogram.filters.chat_member_updated import IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
import asyncpg

# Ініціалізація логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Зчитування змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BASE_URL or not DATABASE_URL:
    raise ValueError("Критична помилка: Відсутні обов'язкові змінні оточення!")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

DB_POOL = None
ADMIN_ID = 124303561

# ==========================================
# БАЗА ДАНИХ
# ==========================================

async def get_db_connection():
    global DB_POOL
    if DB_POOL is None:
        try:
            DB_POOL = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10
            )
            logger.info("Пул БД створено.")
        except Exception as e:
            logger.error(f"Помилка створення пулу БД: {e}")
            raise e
    return DB_POOL

async def init_db():
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                chat_id BIGINT PRIMARY KEY,
                status TEXT DEFAULT 'registration',
                round_number INT DEFAULT 0,
                players JSONB DEFAULT '{}'::jsonb,
                current_word_data JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pro_users (
                user_id BIGINT PRIMARY KEY,
                is_pro BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await conn.execute("""
            ALTER TABLE games ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
        """)
        await conn.execute("""
            ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
        """)
        await conn.execute("""
            ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
        """)

        await conn.execute("UPDATE games SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute("UPDATE pro_users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
        await conn.execute("UPDATE pro_users SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL;")
        
        logger.info("Таблиці БД оновлено.")

async def load_game(chat_id: int):
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, round_number, players, current_word_data FROM games WHERE chat_id = $1",
            chat_id
        )
        if row:
            return {
                "status": row["status"],
                "round_number": row["round_number"],
                "players": json.loads(row["players"]) if isinstance(row["players"], str) else row["players"],
                "current_word_data": json.loads(row["current_word_data"]) if row["current_word_data"] else {}
            }
        return None

async def save_game(chat_id: int, status: str, round_number: int, players: dict, current_word_data: dict = None):
    pool = await get_db_connection()
    players_json = json.dumps(players)
    current_word_json = json.dumps(current_word_data) if current_word_data else "{}"

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO games (chat_id, status, round_number, players, current_word_data)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id)
            DO UPDATE SET status = $2, round_number = $3, players = $4, current_word_data = $5
        """, chat_id, status, round_number, players_json, current_word_json)

async def is_user_pro(user_id: int) -> bool:
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT is_pro FROM pro_users WHERE user_id = $1", user_id)
        return bool(val)

async def set_user_pro_status(user_id: int, status: bool):
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO pro_users (user_id, is_pro, created_at, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id)
            DO UPDATE SET is_pro = $2, updated_at = CURRENT_TIMESTAMP
        """, user_id, status)

# ==========================================
# ВСПУТНІ ФУНКЦІЇ ТА ЛОГІКА ІГРИ
# ==========================================

async def check_group_has_pro(chat_id: int) -> bool:
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        pro_rows = await conn.fetch("SELECT user_id FROM pro_users WHERE is_pro = true")
        pro_user_ids = [row["user_id"] for row in pro_rows]

        if await is_user_pro(ADMIN_ID) and ADMIN_ID not in pro_user_ids:
            pro_user_ids.append(ADMIN_ID)

        for u_id in pro_user_ids:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=u_id)
                if member.status in ["creator", "administrator", "member"]:
                    return True
            except Exception:
                continue
    return False

async def get_chat_players_count(chat_id: int) -> int:
    try:
        count = await bot.get_chat_member_count(chat_id)
        return count
    except Exception as e:
        logger.error(f"Помилка підрахунку гравців: {e}")
        return 0

async def filter_active_players(chat_id: int, players: dict, current_word_data: dict):
    active_players = {}
    was_changed = False
    for p_id, p_info in players.items():
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=int(p_id))
            if member.status not in ["left", "kicked"]:
                active_players[p_id] = p_info
            else:
                was_changed = True
        except Exception:
            active_players[p_id] = p_info

    if was_changed:
        if not current_word_data:
            current_word_data = {}
        current_word_data["composition_changed"] = True

    return active_players, current_word_data

async def check_and_handle_alone(chat_id: int, callback: types.CallbackQuery = None) -> bool:
    count = await get_chat_players_count(chat_id)
    actual_humans = count - 1 if count > 0 else 1

    if actual_humans < 2:
        text = "Щоб грати, додайте в групу другого гравця.\n\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]])
        try:
            if callback:
                await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
                await callback.answer()
            else:
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Помилка повідомлення про соло-гру: {e}")
        return True
    return False

def generate_scoreboard(players: dict) -> str:
    if not players:
        return "player 1: 0\nplayer 2: 0"
    lines = []
    for p in players.values():
        name = p.get("username") or p.get("name") or f"ID {p.get('id')}"
        if not name.startswith("@") and p.get("username"):
            name = f"@{name}"
        lines.append(f"{name}: {p.get('score', 0)}")
    
    if len(lines) == 1:
        lines.append("player 2: 0")
    return "\n".join(lines)

async def send_current_round_post(chat_id: int, game: dict):
    round_num = game["round_number"]
    players = game["players"]
    scoreboard = generate_scoreboard(players)

    if round_num == 1:
        text = f"Раунд 1\n\nРахунок\n{scoreboard}\n\nЗавдання: cфотографуй число 1"
    else:
        text = f"Раунд {round_num}\n\nРахунок\n{scoreboard}\n\nЗавдання: число {round_num}"

    kb_list = []
    if round_num > 1:
        kb_list.append([InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {round_num - 1}", callback_data=f"clear_round_{round_num - 1}")])
    
    kb_list.append([InlineKeyboardButton(text="НОВА ГРА", callback_data="start_free_10")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_list)

    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)

async def show_rules_or_limits(chat_id: int):
    count = await get_chat_players_count(chat_id)
    actual_humans = count - 1 if count > 0 else 1
    has_pro = await check_group_has_pro(chat_id)

    if has_pro:
        if actual_humans > 10:
            text = "Грати може максимум 10 людей.\n\nЩоб перезапустити бота, напишіть в чат команду /start або /play."
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="НАС ВЖЕ 10", callback_data="check_limit_pro")]])
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return
    else:
        if actual_humans >= 3:
            text = "Щоб грати втрьох і більше, хоч 1 гравець має бути pro.\n\nPro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах pro-гравця"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", callback_data="start_pro_buy")],
                [InlineKeyboardButton(text="НАС ВЖЕ ДВОЄ", callback_data="check_limit_free")]
            ])
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return

    text = (
        "Правила гри:\n\n"
        "1. Треба фоткати числа. Хто перший — отримує бал.\n\n"
        "2. 1 раунд = 1 фото.\n"
        "Free: 10 раундів.\n"
        "Pro: 100.\n\n"
        "3. Числа не можна писати. Тільки фото.\n\n"
        "4. Не можна брати числа з однієї локації двічі.\n\n"
        "5. Якщо фото не підходить — раунд можна обнулити.\n\n"
        "Бот реагує лише на фото і кнопки.\n\n"
        "Для рестарту: /start або /play.\n\n"
        "Придумайте приз і гоу!"
    )

    if has_pro:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="start_pro_game_active")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")],
            [InlineKeyboardButton(text="НОВА ГРА ДО 100", callback_data="start_pro_buy")]
        ])

    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_web_page_preview=True)

# ==========================================
# АДМІНІСТРАТИВНА СТАТИСТИКА
# ==========================================

async def get_db_stats_isolated(pool, dt=None):
    time_filter = "AND created_at >= $1" if dt else ""
    params = [dt] if dt else []
    not_only_admin_filter = f"""
        (players = '{{}}'::jsonb OR NOT (players ? '{ADMIN_ID}' AND (SELECT count(*) FROM jsonb_object_keys(players)) = 1))
    """

    sql_chats = f"SELECT COUNT(*) FROM games WHERE {not_only_admin_filter} {time_filter}"
    sql_games_10 = f"SELECT COUNT(*) FROM games WHERE (status='playing_free' OR (status='finished' AND round_number=10) OR (status='registration' AND round_number=0)) AND {not_only_admin_filter} {time_filter}"
    sql_games_100 = f"SELECT COUNT(*) FROM games WHERE (status='playing_pro' OR (status='finished' AND round_number=100)) AND {not_only_admin_filter} {time_filter}"
    sql_users = f"SELECT COUNT(DISTINCT u.key) FROM (SELECT key FROM games, jsonb_each_text(players) WHERE {not_only_admin_filter} {time_filter}) u"
    sql_pro = f"SELECT COUNT(DISTINCT u.key) FROM (SELECT key FROM games, jsonb_each_text(players) WHERE {not_only_admin_filter} {time_filter}) u JOIN pro_users ON pro_users.user_id = u.key::bigint WHERE pro_users.is_pro = true"

    async with pool.acquire() as conn:
        chats = await conn.fetchval(sql_chats, *params) or 0
        games_10 = await conn.fetchval(sql_games_10, *params) or 0
        games_100 = await conn.fetchval(sql_games_100, *params) or 0
        users = await conn.fetchval(sql_users, *params) or 0
        pro = await conn.fetchval(sql_pro, *params) or 0
        free = users - pro if users >= pro else 0

    return (chats, games_10, games_100, users, free, pro)

# ==========================================
# ХЕНДЛЕРИ КОМАНД ТА ЧАТІВ
# ==========================================

@dp.message(F.chat.type == "private", Command("free", "pro"))
async def toggle_admin_status(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        command = message.text.split()[0].replace("/", "").lower()
        if "pro" in command:
            await set_user_pro_status(ADMIN_ID, True)
            await message.reply("Твій статус pro")
        else:
            await set_user_pro_status(ADMIN_ID, False)
            await message.reply("Твій статус free")

@dp.message(F.chat.type == "private", Command("stat"))
async def admin_stat(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    pool = await get_db_connection()
    now = datetime.now()
    try:
        res0, res1, res2, res3, res4 = await asyncio.gather(
            get_db_stats_isolated(pool),
            get_db_stats_isolated(pool, now - timedelta(days=365)),
            get_db_stats_isolated(pool, now - timedelta(days=30)),
            get_db_stats_isolated(pool, now - timedelta(days=7)),
            get_db_stats_isolated(pool, now - timedelta(hours=24))
        )
        stat_text = (
            f"ЗА ВЕСЬ ЧАС:\n"
            f"- всі чати: {res0[0]}\n"
            f"- всі ігри до 10: {res0[1]}\n"
            f"- всі ігри до 100: {res0[2]}\n"
            f"- всі юзери: {res0[3]}\n"
            f"- free: {res0[4]}\n"
            f"- pro: {res0[5]}"
        )
        await message.answer(stat_text)
    except Exception as e:
        logger.error(f"Помилка статистики: {e}")
        await message.answer(f"Помилка статистики: {e}")

@dp.message(F.chat.type == "private")
async def private_stub(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return
    await message.answer("Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). Знайдеш мене по пошуку @stofotobot")

# ВИПРАВЛЕНО: Фільтр оновлено під актуальну версію aiogram 3.x
@dp.my_chat_member(ChatMemberUpdatedFilter(member_registration_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated):
    chat_id = event.chat.id
    await save_game(chat_id, "registration", 0, {})
    await asyncio.sleep(1.5)
    try:
        await show_rules_or_limits(chat_id)
    except Exception as e:
        logger.error(f"Помилка показу правил: {e}")

@dp.chat_member()
async def user_added_to_group(event: ChatMemberUpdated):
    chat_id = event.chat.id
    if event.new_chat_member.status == "member" and event.old_chat_member.status in ["left", "kicked", "restricted"]:
        try:
            await show_rules_or_limits(chat_id)
        except Exception as e:
            logger.error(f"Помилка перевірки лімітів: {e}")

@dp.message(Command("start", "play"))
async def manual_start_in_group(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    chat_id = message.chat.id
    existing_game = await load_game(chat_id)

    players = existing_game["players"] if existing_game else {}
    current_word_data = existing_game["current_word_data"] if existing_game else {}

    players, current_word_data = await filter_active_players(chat_id, players, current_word_data)
    for p_id in players:
        players[p_id]["score"] = 0

    await save_game(chat_id, "registration", 0, players, current_word_data)
    await asyncio.sleep(0.5)
    try:
        await show_rules_or_limits(chat_id)
    except Exception as e:
        logger.error(f"Помилка manual_start_in_group: {e}")

# ==========================================
# ОБРОБНИКИ КНОПОК ТА ІГРОВОГО ПРОЦЕСУ
# ==========================================

@dp.callback_query(F.data == "start_free_10")
async def process_start_free_10(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if await check_and_handle_alone(chat_id, callback):
        return

    game = await load_game(chat_id) or {"players": {}}
    players = game["players"]
    
    # Додаємо ініціатора гри, якщо список пустий
    u_id = str(callback.from_user.id)
    if u_id not in players:
        players[u_id] = {
            "id": callback.from_user.id,
            "name": callback.from_user.full_name,
            "username": callback.from_user.username,
            "score": 0
        }

    new_game = {
        "status": "playing_free",
        "round_number": 1,
        "players": players,
        "current_word_data": {}
    }
    await save_game(chat_id, "playing_free", 1, players, {})
    await callback.message.delete()
    await send_current_round_post(chat_id, new_game)
    await callback.answer()

@dp.callback_query(F.data == "start_pro_game_active")
async def process_start_pro_100(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if await check_and_handle_alone(chat_id, callback):
        return

    game = await load_game(chat_id) or {"players": {}}
    players = game["players"]

    new_game = {
        "status": "playing_pro",
        "round_number": 1,
        "players": players,
        "current_word_data": {}
    }
    await save_game(chat_id, "playing_pro", 1, players, {})
    await callback.message.delete()
    await send_current_round_post(chat_id, new_game)
    await callback.answer()

@dp.callback_query(F.data.startswith("clear_round_"))
async def process_clear_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    try:
        target_round = int(callback.data.split("_")[2])
    except Exception:
        await callback.answer("Помилка обнулення.")
        return

    game = await load_game(chat_id)
    if not game or game["status"] not in ["playing_free", "playing_pro"]:
        await callback.answer("Гра не активна.")
        return

    game["round_number"] = target_round
    await save_game(chat_id, game["status"], target_round, game["players"], game["current_word_data"])
    await callback.message.delete()
    await send_current_round_post(chat_id, game)
    await callback.answer(f"Раунд {target_round} скинуто.")

@dp.callback_query(F.data == "start_pro_buy")
async def process_pro_buy(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = "Pro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах pro-гравця"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=f"https://send.monobank.ua/jar/8Sg7bYg9Xb?a=100&m={user_id}")],
        [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
    ])
    await callback.bot.send_message(chat_id=callback.message.chat.id, text=text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.in_({"check_limit_free", "check_limit_pro"}))
async def process_check_limits(callback: types.CallbackQuery):
    await callback.answer("Перевірка лімітів оновлена.")
    await show_rules_or_limits(callback.message.chat.id)

# ОБРОБНИК ІГРОВИХ ФОТОГРАФІЙ
@dp.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
async def handle_player_photo(message: types.Message):
    chat_id = message.chat.id
    game = await load_game(chat_id)
    
    if not game or game["status"] not in ["playing_free", "playing_pro"]:
        return

    user_id = str(message.from_user.id)
    players = game["players"]

    if user_id not in players:
        players[user_id] = {
            "id": message.from_user.id,
            "name": message.from_user.full_name,
            "username": message.from_user.username,
            "score": 0
        }

    players[user_id]["score"] = players[user_id].get("score", 0) + 1
    current_round = game["round_number"]
    max_rounds = 10 if game["status"] == "playing_free" else 100

    if current_round >= max_rounds:
        # Кінець гри
        winner_name = message.from_user.username
        winner_text = f"@{winner_name}" if winner_name else message.from_user.full_name
        scoreboard = generate_scoreboard(players)
        
        end_text = f"Переможець: {winner_text}\n\nРахунок\n{scoreboard}\n\nНе забудь про свій приз!"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"ОБНУЛИТИ РАУНД {current_round}", callback_data=f"clear_round_{current_round}")],
            [InlineKeyboardButton(text="НОВА ГРА", callback_data="start_free_10")]
        ])
        await save_game(chat_id, "finished", current_round, players, {})
        await message.answer(end_text, reply_markup=kb)
    else:
        # Наступний раунд
        game["round_number"] += 1
        await save_game(chat_id, game["status"], game["round_number"], players, {})
        await send_current_round_post(chat_id, game)

# ==========================================
# WEBHOOKS & FASTAPI LIFESPAN
# ==========================================

app = FastAPI()

@app.post("/webhook")
async def bot_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    try:
        update = types.Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Помилка обробки апдейту: {e}")

    return Response(status_code=200)

@app.post("/mono_webhook")
async def mono_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    if data.get("type") == "StatementItem":
        statement = data.get("data", {}).get("statementItem", {})
        comment = statement.get("comment", "")
        amount = statement.get("amount", 0)

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
                    text = "Pro-версія гри:\n- до 10 гравців\n- до 100 раундів назавжди\n- у всіх чатах pro-гравця"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="КУПИТИ PRO-ВЕРСІЮ", url=f"https://send.monobank.ua/jar/8Sg7bYg9Xb?a=100&m={user_id}")],
                        [InlineKeyboardButton(text="НОВА ГРА ДО 10", callback_data="start_free_10")]
                    ])
                    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
                except Exception as e:
                    logger.error(f"Помилка повідомлення: {e}")

    return Response(status_code=200)

@app.get("/")
async def root():
    return JSONResponse(
        content={"status": "working", "bot": "100_photo_bot"},
        headers={"Content-Type": "application/json; charset=utf-8"}
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    logger.info("Webhook успішно встановлено!")
    
    yield
    
    logger.info("Закриття додатка, очищення ресурсів...")
    await dp.storage.close()
    if bot.session:
        await bot.session.close()
        logger.info("Сесію бота закрито.")
    if DB_POOL:
        await DB_POOL.close()
        logger.info("Пул БД закрито.")

app.router.lifespan_context = lifespan
