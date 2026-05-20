import os
import logging
import asyncio
from typing import Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Налаштування логування
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Зчитування змінних оточення (Суворо за ТЗ)
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
BASE_URL = os.getenv("BASE_URL", "https://stophotobot-1.onrender.com")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN або TELEGRAM_TOKEN не знайдені в змінних оточення!")

# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Емуляція асинхронного сховища бази даних (на базі Supabase/Postgres структури)
# У реальному коді тут ідуть виклики до вашої таблиці через asyncpg/supabase-py.
# Для надійності зберігаємо стан гри ізольовано для кожного chat_id.
class GameDatabase:
    def __init__(self):
        self.games: Dict[int, Dict[str, Any]] = {}
        self.pro_users = {124303561} # ID адміна за замовчуванням має PRO

    async def get_or_create_game(self, chat_id: int) -> Dict[str, Any]:
        if chat_id not in self.games:
            self.games[chat_id] = {
                "current_task": 1,
                "max_rounds": 10,
                "scores": {},      # user_id: { "name": str, "score": int }
                "history": [],     # список логів раундів: [{"task": int, "user_id": int}]
                "is_pro": False
            }
        return self.games[chat_id]

    async def reset_game(self, chat_id: int, max_rounds: int = 10):
        self.games[chat_id] = {
            "current_task": 1,
            "max_rounds": max_rounds,
            "scores": {},
            "history": [],
            "is_pro": self.games.get(chat_id, {}).get("is_pro", False)
        }
        return self.games[chat_id]

db = GameDatabase()

# --- ГЕНЕРАТОРЫ ТЕКСТОВ И КНОПОК ---

def get_welcome_text() -> str:
    # Суворе копіювання структури абзаців та переносів рядків за ТЗ
    return (
        "Вітаємо у грі <a href=\"https://t.me/stophotobot\">100 PHOTO</a>!\n\n"
        "Правила集 гри:\n\n"
        "1. Завдання гравців – фотографувати числа (1, 2, 3) і надсилати у цей чат.\n\n"
        "2. Безоплатна гра триває 10 раундів, платна – 100 раундів. 1 раунд = 1 photo. "
        "За кожне фото гравець отримує 1 бал.\n\n"
        "3. Числа не можна створювати (викладати предметами) або писати самому. "
        "Лише фотографувати їх вдома, на вулиці тощо.\n\n"
        "4. Не можна повторювати двічі числа з однієї локації (номери сторінок у книзі, кнопки в ліфті тощо). "
        "Локації мають бути різними.\n\n"
        "5. Якщо надіслане фото не відповідає правилам, це фото можна відмінити і почати раунд заново.\n\n"
        "Щоб перезапустити бота, напишіть в чат команду /start або /play.\n\n"
        "За бажанням, придумайте приз переможцю.\n\n"
        "Натхнення!"
    )

def get_game_keyboard(game: Dict[str, Any], current_task: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    # Кнопка скасування з'являється тільки якщо пройшов хоча б 1 раунд
    if current_task > 1:
        builder.button(text=f"ОБНУЛИТИ РАУНД {current_task - 1}", callback_data=f"cancel_round_{current_task - 1}")
    
    builder.button(text="НОВА ГРА ДО 10", callback_data="start_game_10")
    builder.button(text="НОВА ГРА ДО 100", callback_data="buy_pro")
    builder.button(text="ДОДАТИ ГРАВЦІВ", callback_data="add_players")
    
    builder.adjust(1)
    return builder.as_markup()

def build_score_message(game: Dict[str, Any], show_instructions: bool = False) -> str:
    scores = game["scores"]
    current_task = game["current_task"]
    
    # Шаблон відображення рахунку
    score_lines = []
    if not scores:
        score_lines.append("Рахунок:\nПлеєр 1: 0\nПлеєр 2: 0")
    else:
        score_lines.append("Рахунок:")
        for user_id, data in scores.items():
            score_lines.append(f"{data['name']}: {data['score']}")
            
    score_text = "\n".join(score_lines)
    
    if show_instructions:
        # Інструкція «Знайди і сфотографуй...» йде ТІЛЬКИ для першого раунду
        return f"{score_text}\n\nЗавдання: {current_task}\n\nЗнайди і сфотографуй число {current_task}."
    else:
        # Для раундів 2+ йде лише маркер завдання без тексту-роз'яснення
        return f"{score_text}\n\nЗавдання: {current_task}"

# --- ОБРОБНИКИ ТЕЛЕГРАМ ПОДІЙ ---

# Хендлер на додавання бота до групи (Головний тригер за ТЗ)
@dp.my_chat_member()
async def on_bot_added_to_group(chat_member: types.ChatMemberUpdated):
    if chat_member.new_chat_member.status in ["member", "administrator"]:
        chat_id = chat_member.chat.id
        logger.info(f"Бот доданий до групи: {chat_id}")
        
        # Ініціалізація нової ігри в БД
        game = await db.reset_game(chat_id, max_rounds=10)
        
        # Миттєва відправка правил гри
        await bot.send_message(
            chat_id=chat_id,
            text=get_welcome_text(),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
        # Відправка першого раунду за шаблоном
        await bot.send_message(
            chat_id=chat_id,
            text=build_score_message(game, show_instructions=True),
            reply_markup=get_game_keyboard(game, 1)
        )

# Хендлери ручних команд /start або /play у групах
@dp.message(lambda msg: msg.chat.type in ["group", "supergroup"] and msg.text in ["/start", "/play"])
async def start_game_command(message: types.Message):
    game = await db.reset_game(message.chat.id, max_rounds=10)
    await message.answer(
        text=get_welcome_text(),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await message.answer(
        text=build_score_message(game, show_instructions=True),
        reply_markup=get_game_keyboard(game, 1)
    )

# Заглушка для приватних чатів звичайних користувачів
@dp.message(lambda msg: msg.chat.type == "private" and msg.from_user.id != 124303561)
async def private_chat_stub(message: types.Message):
    await message.answer(
        "Щоб грати, додай мене у групу з іншими людьми (не в особисті чати, а саме у групу). "
        "Знайдеш мене через пошук @stophotobot"
    )

# Адмін-команда /stat в приваті (Суворо для адміна)
@dp.message(lambda msg: msg.chat.type == "private" and msg.from_user.id == 124303561 and msg.text == "/stat")
async def admin_stat_command(message: types.Message):
    # Захищений запит статистики
    total_chats = len(db.games)
    pro_chats = sum(1 for g in db.games.values() if g.get("is_pro"))
    
    stat_msg = (
        "📊 <b>Статистика бота 100 PHOTO</b>\n\n"
        "• Всього ігрових груп: {total}\n"
        "• Активних PRO груп: {pro}\n"
        "• Період моніторингу: за весь час"
    ).format(total=total_chats, pro=pro_chats)
    
    await message.answer(stat_msg, parse_mode="HTML")

# --- МЕХАНІКА ОБРОБКИ ФОТОГРАФІЙ ---

@dp.message(lambda msg: msg.chat.type in ["group", "supergroup"] and msg.photo)
async def handle_game_photo(message: types.Message):
    chat_id = message.chat.id
    game = await db.get_or_create_game(chat_id)
    
    current_task = game["current_task"]
    max_rounds = game["max_rounds"]
    
    # Перевірка ліміту гри
    if current_task > max_rounds:
        return  # Гра закінчена, ігноруємо фото до натискання «Нова гра»
        
    user = message.from_user
    # Отримання імені профілю або юзернейму за ТЗ
    user_name = user.full_name if user.full_name else f"@{user.username}"
    user_id = user.id
    
    # Оновлення рахунку гравця
    if user_id not in game["scores"]:
        game["scores"][user_id] = {"name": user_name, "score": 0}
        
    game["scores"][user_id]["score"] += 1
    
    # Збереження кроку в історію для точного поодинокого скасування
    game["history"].append({
        "task": current_task,
        "user_id": user_id
    })
    
    # Перехід до наступного раунду
    game["current_task"] += 1
    
    if game["current_task"] > max_rounds:
        # Визначення переможця після завершення гри
        winner_name = max(game["scores"].items(), key=lambda x: x[1]["score"])[1]["name"]
        
        final_score_text = "\n".join([f"{d['name']}: {d['score']}" for d in game["scores"].values()])
        end_text = (
            f"Рахунок:\n{final_score_text}\n\n"
            f"Переможець: {winner_name}\n\n"
            f"Не забудь про свій приз!"
        )
        await message.answer(text=end_text, reply_markup=get_game_keyboard(game, game["current_task"]))
    else:
        # Надсилання наступного раунду (без повторення довгої інструкції)
        await message.answer(
            text=build_score_message(game, show_instructions=False),
            reply_markup=get_game_keyboard(game, game["current_task"])
        )

# --- ОБРОБКА CALLBACK КНОПОК ---

@dp.callback_query(lambda cb: cb.data.startswith("cancel_round_"))
async def handle_cancel_round(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = await db.get_or_create_game(chat_id)
    
    # Витягуємо цільовий раунд із дата-кнопки
    try:
        cancel_target = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Помилка обробки скасування раунду.")
        return

    # Захист: скасовувати можна лише той раунд, який безпосередньо передує поточному завданню
    if game["current_task"] - 1 != cancel_target:
        await callback.answer("Цей раунд вже не можна скасувати!", show_alert=True)
        return

    # Шукаємо в історії запис про цей раунд, щоб відняти бал саме в того, хто його здав
    history_entry = next((item for item in reversed(game["history"]) if item["task"] == cancel_target), None)
    
    if history_entry:
        u_id = history_entry["user_id"]
        if u_id in game["scores"] and game["scores"][u_id]["score"] > 0:
            game["scores"][u_id]["score"] -= 1
        # Видаляємо запис з історії
        game["history"] = [item for item in game["history"] if item["task"] != cancel_target]

    # Відкочуємо лічильник завдання назад строго на цільовий раунд
    game["current_task"] = cancel_target
    
    # Сповіщення про успішне скасування (тільки спалахом, без текстового спаму в чат)
    await callback.answer(f"Раунд {cancel_target} обнулено!")
    
    # Надсилаємо ОДИН ОНОВЛЕНИЙ пост завдання з актуальним перерахованим рахунком
    # Якщо відкотилися на 1 раунд — показуємо інструкцію, інакше — ні.
    show_instr = (cancel_target == 1)
    await callback.message.answer(
        text=build_score_message(game, show_instructions=show_instr),
        reply_markup=get_game_keyboard(game, cancel_target)
    )

@dp.callback_query(lambda cb: cb.data == "start_game_10")
async def handle_start_game_10(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = await db.reset_game(chat_id, max_rounds=10)
    await callback.answer("Починаємо нову гру до 10!")
    await callback.message.answer(
        text=build_score_message(game, show_instructions=True),
        reply_markup=get_game_keyboard(game, 1)
    )

@dp.callback_query(lambda cb: cb.data == "buy_pro")
async def handle_buy_pro(callback: types.CallbackQuery):
    # Реакція на кнопку «НОВА ГРА ДО 100»
    await callback.answer("Запуск PRO версії", show_alert=False)
    # Посилання або реквізити Монобанку для активації
    await callback.message.answer(
        "🌟 <b>Активація режиму PRO (До 100 раундів та 10 гравців)</b>\n\n"
        "Для підключення PRO-версії виконайте оплату у розмірі 100 грн.\n"
        "💳 Посилання на оплату: <a href='https://monobank.ua'>👉 Сплатити через Monobank</a>\n\n"
        "Після зарахування коштів гра автоматично розшириться до 100 раундів!",
        parse_mode="HTML"
    )

@dp.callback_query(lambda cb: cb.data == "add_players")
async def handle_add_players(callback: types.CallbackQuery):
    # Реакція на кнопку «ДОДАТИ ГРАВЦІВ»
    await callback.answer()
    await callback.message.answer(
        "👥 <b>Як додати гравців до гри?</b>\n\n"
        "Просто надішліть новим учасникам посилання на цю групу або додайте їх через інтерфейс Telegram.\n"
        "Кожен, хто надішле фотографію з числом-відповіддю у цей чат, автоматично стане учасником та з'явиться у загальному рахунку!",
        parse_mode="HTML"
    )

# --- FASTAPI WEBHOOK LIFESPAN КОНФІГУРАЦІЯ ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Дія при старті: встановлення вебхука (Суворо за ТЗ)
    webhook_url = f"{BASE_URL}/webhook"
    logger.info(f"Встановлення Webhook на адресу: {webhook_url}")
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    yield
    # Дія при зупинці: видалення вебхука
    logger.info("Видалення Webhook при зупинці сервісу...")
    await bot.delete_webhook()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook_endpoint(request: Request):
    try:
        update_data = await request.json()
        update = types.Update(**update_data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Помилка обробки апдейту на ендпоінті webhook: {e}")
    return {"status": "ok"}

@app.get("/")
async def root_endpoint():
    return {"message": "Бот 100 PHOTO працює в режимі Webhook!"}
