import os
import logging
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
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

# Константи конфігурації
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 453664724  # Твій Telegram ID для команди /stat

# Прямі лінки на картинки у твоєму репозиторії Render
PHOTO_RULES = "https://stophotobot.onrender.com/1.png"
PHOTO_START = "https://stophotobot.onrender.com/2.png"
PHOTO_END = "https://stophotobot.onrender.com/3.png"

# Підключення до бази даних Supabase (PostgreSQL)
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# Ініціалізація користувача в базі
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

# Перевірка PRO статусу (хоча б один користувач у чаті має бути PRO)
def db_is_chat_pro(chat_id):
    # Для простоти, якщо гра створена як PRO або ініціатор PRO, зберігаємо стан в базі
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM games WHERE chat_id = %s", (chat_id,))
                res = cur.fetchone()
                if res and 'PRO' in res['state']:
                    return True
    except Exception as e:
        logger.error(f"Помилка перевірки PRO статусу: {e}")
    return False

# Отримання стану гри
def db_get_game(chat_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM games WHERE chat_id = %s", (chat_id,))
                return cur.fetchone()
    except Exception as e:
        logger.error(f"Помилка get_game: {e}")
    return None

# Збереження/Оновлення стану гри
def db_save_game(chat_id, state, max_rounds, current_round, scores, history):
    import json
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

# Функція генерації тексту рахунку
def render_scores(scores_dict, is_round_one=False):
    if is_round_one and not scores_dict:
        return "@user1: 0\n@user2: 0"
    if not scores_dict:
        return "Немає активних гравців"
    
    # Сортуємо за спаданням балів
    sorted_scores = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)
    return "\n".join([f"{user}: {score}" for user, score in sorted_scores])

# Команда /start або /play
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    # Зберігаємо юзера в базу
    db_upsert_user(user.id, user.username, user.full_name)
    
    # Текст правил з гіперпосиланням
    rules_text = (
        "Вітаємо у грі <a href='https://t.me/stophotobot'>100 PHOTO</a>!\n\n"
        "Правила гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n"
        "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 photo.\n"
        "За кожне фото гравець отримує 1 бал.\n\n"
        "3. Числа не можна створювати (викладати предметами) або писати самому. "
        "Лише фотографувати їх вдома, на вулиці тощо.\n\n"
        "4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо). "
        "Локації мають бути різними.\n\n"
        "5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\n"
        "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
        "За бажанням, придумайте приз переможцю.\n\n"
        "Натхнення!"
    )
    
    # Кнопки для повідомлення
    keyboard = [
        [InlineKeyboardButton("[ НОВА ГРА ДО 10 ]", callback_data="new_game_10")],
        [InlineKeyboardButton("[ НОВА ГРА ДО 100 ]", callback_data="buy_pro")],
        [InlineKeyboardButton("[ ДОДАТИ ГРАВЦІВ ]", callback_data="add_players")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await chat.send_photo(
        photo=PHOTO_RULES,
        caption=rules_text,
        parse_mode="HTML",
        reply_markup=reply_markup
    )

# Обробка натискань на кнопки
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    user = query.from_user
    data = query.data
    
    # Зберігаємо того, хто натиснув кнопку
    db_upsert_user(user.id, user.username, user.full_name)
    
    if data == "new_game_10":
        # Ініціалізуємо FREE гру до 10 раундів
        db_save_game(chat_id, "RUNNING_FREE", 10, 1, {}, [])
        
        caption = (
            "Завдання: 1\n\n"
            "Рахунок\n"
            f"{render_scores({}, is_round_one=True)}\n\n"
            "Знайди і сфотографуй число 1."
        )
        keyboard = [
            [InlineKeyboardButton("[ ОБНУЛИТИ РАУНД 0 ]", callback_data="void_round")],
            [InlineKeyboardButton("[ НОВА ГРА ДО 10 ]", callback_data="new_game_10")],
            [InlineKeyboardButton("[ НОВА ГРА ДО 100 ]", callback_data="buy_pro")],
            [InlineKeyboardButton("[ ДОДАТИ ГРАВЦІВ ]", callback_data="add_players")]
        ]
        await query.message.reply_photo(
            photo=PHOTO_START,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif data == "buy_pro":
        # Екрани оплати
        caption = (
            "Pro-версія гри:\n"
            "- до 10 гравців\n"
            "- до 100 раундів назавжди\n"
            "- у всіх чатах Pro-гравця"
        )
        keyboard = [
            [InlineKeyboardButton("[ КУПИТИ PRO-ВЕРСІЮ ]", callback_data="success_pay")], # Тимчасово для тесту переводить на успіх
            [InlineKeyboardButton("[ ПРОДОВЖИТИ ГРУ УДВОХ ]", callback_data="new_game_10")]
        ]
        await query.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "success_pay":
        # Емуляція успішної оплати, ставимо користувачу PRO статус у базі
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET is_pro = TRUE WHERE telegram_id = %s", (user.id,))
                    conn.commit()
        except Exception as e:
            logger.error(e)
            
        caption = (
            "Дякую, оплата є!\n"
            f"– @{user.username or user.first_name} тепер Pro\n"
            "– відкрито 100 раундів\n"
            "– відкрито 10 гравців"
        )
        keyboard = [[InlineKeyboardButton("[ НОВА ГРА ]", callback_data="new_game_pro")]]
        await query.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "new_game_pro":
        # Запуск PRO гри
        db_save_game(chat_id, "RUNNING_PRO", 100, 1, {}, [])
        caption = (
            "Завдання: 1\n\n"
            "Рахунок\n"
            f"{render_scores({}, is_round_one=True)}\n\n"
            "Знайди і сфотографуй число 1."
        )
        keyboard = [
            [InlineKeyboardButton("[ ОБНУЛИТИ РАУНД 0 ]", callback_data="void_round")],
            [InlineKeyboardButton("[ ПОЧАТИ ЗАНОВО ]", callback_data="new_game_pro")]
        ]
        await query.message.reply_photo(
            photo=PHOTO_START,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif data == "void_round":
        game = db_get_game(chat_id)
        if not game or game['state'] == 'IDLE':
            await query.message.reply_text("Немає активної гри для скасування раунду.")
            return
            
        current_round = game['current_round']
        scores = game['scores'] or {}
        history = game['history'] or []
        
        if not history:
            await query.message.reply_text("Немає дій для скасування в цьому раунді.")
            return
            
        # Скасовуємо останній бал з історії раунду
        last_action = history.pop()
        last_user = last_action.get('user')
        
        if last_user in scores and scores[last_user] > 0:
            scores[last_user] -= 1
            if scores[last_user] == 0:
                del scores[last_user]
                
        # Знижуємо раунд назад, якщо він уже встиг перемкнутися
        prev_round = max(1, current_round - 1)
        
        db_save_game(chat_id, game['state'], game['max_rounds'], prev_round, scores, history)
        
        await query.message.reply_text(f"Раунд {prev_round} було обнулено! Надішліть правильне фото заново.")

    elif data == "add_players":
        await query.message.reply_text("Щоб додати гравців, просто перешліть їм лінк на цей чат або додайте їх безпосередньо у групу.")

# Обробка фотографій від користувачів
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    game = db_get_game(chat_id)
    if not game or game['state'] == 'IDLE':
        return # Гри немає, ігноруємо фото
        
    state = game['state']
    max_rounds = game['max_rounds']
    current_round = game['current_round']
    scores = game['scores'] or {}
    history = game['history'] or []
    
    # Визначаємо ім'я користувача для виведення на екран
    user_display = user.first_name if user.first_name else f"@{user.username}"
    
    # Нараховуємо 1 бал
    scores[user_display] = scores.get(user_display, 0) + 1
    
    # Додаємо в історію для можливості скасування
    history.append({'round': current_round, 'user': user_display, 'time': datetime.now().isoformat()})
    
    if current_round >= max_rounds:
        # Кінець гри
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
        await update.message.reply_photo(photo=PHOTO_END, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # Наступний раунд
        next_round = current_round + 1
        db_save_game(chat_id, state, max_rounds, next_round, scores, history)
        
        caption = (
            f"Завдання: {next_round}\n\n"
            f"{render_scores(scores)}\n\n"
            f"[ ОБНУЛИТИ РАУНД {next_round-1} ]\n"
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
            
        await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard))

# Команда /stat для адміна
async def stat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return # Доступ лише для адміна
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Збір метрик з бази даних
                cur.execute("SELECT COUNT(*) FROM games")
                total_chats = cur.fetchone()['count']
                
                cur.execute("SELECT COUNT(*) FROM users")
                total_users = cur.fetchone()['count']
                
                cur.execute("SELECT COUNT(*) FROM users WHERE is_pro = TRUE")
                pro_users = cur.fetchone()['count']
                
                free_users = total_users - pro_users
                
                stat_text = (
                    "📊 СТАТИСТИКА БОТА\n\n"
                    f"Всього чатів: {total_chats}\n"
                    f"Всього користувачів: {total_users}\n"
                    f"Безкоштовних користувачів: {free_users}\n"
                    f"PRO користувачів: {pro_users}\n\n"
                    "*(Дані накопичуються з моменту запуску Supabase)*"
                )
                await update.message.reply_text(stat_text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Помилка збору статистики: {e}")

def main():
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler(["start", "play"], start_command))
    application.add_handler(CommandHandler("stat", stat_command))
    application.add_handler(CallbackQueryHandler(button_click))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Запуск довгого опитування (Long Polling)
    application.run_polling()

if __name__ == '__main__':
    main()
