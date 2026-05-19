import os
import logging
import asyncio
import threading
import json
from http.server import SimpleHTTPRequestHandler, HTTPServer
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
    ContextTypes
)
import psycopg2
from psycopg2.extras import RealDictCursor

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен та конфігурація з Render env
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 453664724  # Твій Telegram ID

# Прямі лінки на картинки
PHOTO_RULES = "https://stophotobot.onrender.com/1.png"
PHOTO_START = "https://stophotobot.onrender.com/2.png"
PHOTO_END = "https://stophotobot.onrender.com/3.png"

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
def run_http_server():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            return

    server_address = ('', PORT)
    httpd = HTTPServer(server_address, QuietHandler)
    logger.info(f"Вбудований веб-сервер запущено на порту {PORT}")
    httpd.serve_forever()

# Підключення до бази даних
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def db_upsert_user(telegram_id, username, full_name):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (telegram_id, username, full_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id) 
                    DO UPDATE SET username = EXCLUDED.username, full_name = EXCLUDED.full_name;
                """, (telegram_id, username, full_name))
                conn.commit()
    except Exception as e:
        logger.error(f"Помилка upsert_user: {e}")

def db_get_game(chat_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM games WHERE chat_id = %s", (chat_id,))
                return cur.fetchone()
    except Exception as e:
        logger.error(f"Помилка get_game: {e}")
    return None

def db_save_game(chat_id, state, max_rounds, current_round, scores, history):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO games (chat_id, state, max_rounds, current_round, scores, history)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chat_id) 
                    DO UPDATE SET state = EXCLUDED.state, max_rounds = EXCLUDED.max_rounds,
                                  current_round = EXCLUDED.current_round, scores = EXCLUDED.scores,
                                  history = EXCLUDED.history;
                """, (chat_id, state, max_rounds, current_round, json.dumps(scores), json.dumps(history)))
                conn.commit()
    except Exception as e:
        logger.error(f"Помилка save_game: {e}")

# Генерація списку рахунку без зайвих пробілів
def render_scores(scores_dict, is_round_one=False):
    if is_round_one:
        return "player 1: 0\nplayer 2: 0"
    if not scores_dict:
        return ""
    
    sorted_scores = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)
    return "\n".join([f"{user}: {score}" for user, score in sorted_scores])

# Пост "ПРАВИЛА"
async def send_welcome_rules(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    rules_text = (
        "Вітаємо у грі <a href='https://t.me/stophotobot'>100 PHOTO</a>!\n\n"
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) i надсилати у цей чат.\n\n"
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
    
    keyboard = [
        [InlineKeyboardButton("[ НОВА ГРА ДО 10 ]", callback_data="new_game_10")],
        [InlineKeyboardButton("[ НОВА ГРА ДО 100 ]", callback_data="buy_pro")],
        [InlineKeyboardButton("[ ДОДАТИ ГРАВЦІВ ]", callback_data="add_players")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await context.bot.send_photo(chat_id=chat_id, photo=PHOTO_RULES, caption=rules_text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Помилка відправки правил: {e}")
        await context.bot.send_message(chat_id=chat_id, text=rules_text, parse_mode="HTML", reply_markup=reply_markup)

# Команди старта
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db_upsert_user(user.id, user.username, user.full_name)
    await send_welcome_rules(context, chat_id)

# Автододавання в групу
async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if result.new_chat_member.status in ["member", "administrator"]:
        logger.info(f"Бота додано в чат: {update.effective_chat.id}")
        await send_welcome_rules(context, update.effective_chat.id)

# Обробка кліків кнопок
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    user = query.from_user
    data = query.data
    
    db_upsert_user(user.id, user.username, user.full_name)
    
    if data == "new_game_10":
        db_save_game(chat_id, "RUNNING_FREE", 10, 1, {}, [])
        
        # ПОСТ "ЗАВДАННЯ 1" — як окреме чисте повідомлення без reply та без кнопок
        caption = (
            "Завдання: 1\n\n"
            "Рахунок\n"
            f"{render_scores({}, is_round_one=True)}\n\n"
            "Знайди і сфотографуй число 1."
        )
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=PHOTO_START, caption=caption)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=caption)
        
    elif data == "buy_pro":
        caption = (
            "Pro-версія гри:\n"
            "- до 10 гравців\n"
            "- до 100 раундів назавжди\n"
            "- у всіх чатах Pro-гравця"
        )
        keyboard = [
            [InlineKeyboardButton("[ КУПИТИ PRO-ВЕРСІЮ ]", callback_data="success_pay")],
            [InlineKeyboardButton("[ ПРОДОВЖТИ ГРУ УДВОХ ]", callback_data="new_game_10")]
        ]
        await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "success_pay":
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET is_pro = TRUE WHERE telegram_id = %s", (user.id,))
                    conn.commit()
        except Exception as e:
            logger.error(e)
            
        display_name = f"@{user.username}" if user.username else user.first_name
        caption = (
            "Дякую, оплата є!\n\n"
            f"– {display_name} тепер Pro\n"
            "– відкрито 100 раундів\n"
            "– відкрито 10 гравців\n\n"
            "[ НОВА ГРА ]"
        )
        keyboard = [[InlineKeyboardButton("[ НОВА ГРА ]", callback_data="new_game_pro")]]
        await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "new_game_pro":
        db_save_game(chat_id, "RUNNING_PRO", 100, 1, {}, [])
        caption = (
            "Завдання: 1\n\n"
            "Рахунок\n"
            f"{render_scores({}, is_round_one=True)}\n\n"
            "Знайди і сфотографуй число 1."
        )
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=PHOTO_START, caption=caption)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=caption)
        
    elif data == "void_round":
        game = db_get_game(chat_id)
        if not game or game['state'] == 'IDLE':
            return
            
        current_round = game['current_round']
        scores = game['scores'] or {}
        history = game['history'] or []
        
        if not history:
            return
            
        last_action = history.pop()
        last_user = last_action.get('user')
        
        if last_user in scores and scores[last_user] > 0:
            scores[last_user] -= 1
            if scores[last_user] == 0:
                del scores[last_user]
                
        prev_round = max(1, current_round - 1)
        db_save_game(chat_id, game['state'], game['max_rounds'], prev_round, scores, history)
        await context.bot.send_message(chat_id=chat_id, text=f"Раунд {prev_round} було обнулено! Надішліть правильне фото заново.")

    elif data == "add_players":
        await context.bot.send_message(chat_id=chat_id, text="Щоб додати гравців, просто додайте їх безпосередньо у групу Telegram.")

# Прийом та обробка надісланих фотографій
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    game = db_get_game(chat_id)
    if not game or game['state'] == 'IDLE':
        return
        
    state = game['state']
    max_rounds = game['max_rounds']
    current_round = game['current_round']
    scores = game['scores'] or {}
    history = game['history'] or []
    
    user_display = f"@{user.username}" if user.username else user.first_name
    
    scores[user_display] = scores.get(user_display, 0) + 1
    history.append({'round': current_round, 'user': user_display, 'time': datetime.now().isoformat()})
    
    if current_round >= max_rounds:
        winner = max(scores, key=scores.get) if scores else user_display
        caption = (
            f"Переможець: {winner}\n\n"
            "Рахунок:\n"
            f"{render_scores(scores)}\n\n"
            "Не забудь про свій приз!"
        )
        if "FREE" in state:
            keyboard = [
                [InlineKeyboardButton(f"[ ОБНУЛИТИ РАУНД {current_round} ]", callback_data="void_round")],
                [InlineKeyboardButton("[ НОВА ГРА ДО 10 ]", callback_data="new_game_10")],
                [InlineKeyboardButton("[ НОВА ГРА ДО 100 ]", callback_data="buy_pro")],
                [InlineKeyboardButton("[ ДОДАТИ ГРАВЦІВ ]", callback_data="add_players")]
            ]
        else:
            keyboard = [
                [InlineKeyboardButton(f"[ ОБНУЛИТИ РАУНД {current_round} ]", callback_data="void_round")],
                [InlineKeyboardButton("[ НОВА ГРА ]", callback_data="new_game_pro")]
            ]
            
        db_save_game(chat_id, "IDLE", max_rounds, current_round, scores, history)
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=PHOTO_END, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        next_round = current_round + 1
        db_save_game(chat_id, state, max_rounds, next_round, scores, history)
        
        # ПОСТ "ЗАВДАННЯ 2-9" — надсилається як чисте повідомлення (без reply)
        caption = (
            f"Завдання: {next_round}\n\n"
            f"{render_scores(scores)}\n\n"
            f"Знайди і сфотографуй число {next_round}."
        )
        if "FREE" in state:
            keyboard = [
                [InlineKeyboardButton(f"[ ОБНУЛИТИ РАУНД {next_round-1} ]", callback_data="void_round")],
                [InlineKeyboardButton("[ НОВА ГРА ДО 10 ]", callback_data="new_game_10")],
                [InlineKeyboardButton("[ НОВА ГРА ДО 100 ]", callback_data="buy_pro")],
                [InlineKeyboardButton("[ ДОДАТИ ГРАВЦІВ ]", callback_data="add_players")]
            ]
        else:
            keyboard = [
                [InlineKeyboardButton(f"[ ОБНУЛИТИ РАУНД {next_round-1} ]", callback_data="void_round")],
                [InlineKeyboardButton("[ ПОЧАТИ ЗАНОВО ]", callback_data="new_game_pro")]
            ]
            
        await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=InlineKeyboardMarkup(keyboard))

# Адмін статистика /stat
async def stat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM games")
                total_chats = cur.fetchone()['count']
                cur.execute("SELECT COUNT(*) FROM users")
                total_users = cur.fetchone()['count']
                cur.execute("SELECT COUNT(*) FROM users WHERE is_pro = TRUE")
                pro_users = cur.fetchone()['count']
                
                stat_text = (
                    "📊 СТАТИСТИКА БОТА\n\n"
                    f"Всього чатів: {total_chats}\n\n"
                    f"Всього користувачів: {total_users}\n\n"
                    f"Безкоштовних користувачів: {total_users - pro_users}\n\n"
                    f"PRO користувачів: {pro_users}"
                )
                await context.bot.send_message(chat_id=update.effective_chat.id, text=stat_text)
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Помилка: {e}")

def main():
    threading.Thread(target=run_http_server, daemon=True).start()

    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler(["start", "play"], start_command))
    application.add_handler(CommandHandler("stat", stat_command))
    application.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(button_click))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    application.run_polling()

if __name__ == '__main__':
    main()
