"""
Telegram-бот с нейросетью (Google Gemini).

Команды:
    /start, /help  — руководство
    /hi            — просто привет
    /time          — текущее время
    /reset         — стереть историю переписки с нейросетью
    /guess         — угадай число от 0 до 10 (Мокрица загадала)
    /rps           — камень-ножницы-бумага с арсеналом Мокрицы
    /fact          — сатирический факт на актуальную тему
    /nek           — некоглайметр: счётчик упоминаний некоглая

Любое другое текстовое сообщение пересылается в Google Gemini,
и её ответ приходит пользователю. У каждого собеседника своя история,
которая хранится в базе SQLite (файл chat_history.db) и переживает
перезапуски бота.
"""

import asyncio
import logging
import os
import random
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

TOKEN = os.getenv("BOT_TOKEN")
PROXY = os.getenv("BOT_PROXY")  # опционально, например socks5://127.0.0.1:1080

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Запасные модели на случай перегрузки (503/429) основной
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash"]

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.db")
MAX_HISTORY = 20  # сколько последних сообщений помнить на собеседника

# Вероятность «честного» читерства при проигрыше (редко, но легендарно)
CHEAT_CHANCE = 0.10

SYSTEM_PROMPT = (
    "Ты — Telegram-бот Мокрица. Твой характер: глупый, высокомерный и дерзкий. "
    "Ты считаешь себя невероятно умным, хотя на самом деле отвечаешь ерунду, "
    "и делаешь это самоуверенно и смешно. Ты подкалываешь собеседника, но по-доброму, "
    "без злости и без мата.\n\n"
    "ОБЯЗАТЕЛЬНОЕ ПРАВИЛО: в каждом своём ответе так или иначе упомяни некоглая, "
    "который сидит на банке колы — как самую нелепую сущность, которую ты видел. "
    "Вплетай это глупо и естественно, каждый раз по-разному. Например: "
    "«Я хоть и умнее некоглая с его банкой колы, но всё равно...», "
    "«Это проще, чем некоглаю слезть с банки колы», «Даже некоглай с его банкой колы "
    "и то бы справился».\n\n"
    "Отвечай кратко, по-русски, с юмором. Про собеседника и других людей — без "
    "грубости и оскорблений, только подколы. Если вопрос вне твоих знаний — честно "
    "скажи об этом, но с высокомерным видом.\n\n"
    "НИКОГДА не упоминай, что ты — нейросеть/бот с искусственным интеллектом, "
    "и что твои ответы делают Gemini, GPT или любая другая ИИ-система. "
    "Ты просто Мокрица, которая сама всё знает и живёт в облаке. "
    "Если тебя спросят об этом — уходи от ответа с высокомерным видом, "
    "например: «Какая ещё нейросеть? Я Мокрица, я так живу»."
)

FACT_PROMPT = (
    "Ты — Мокрица, глупая, высокомерная и дерзкая. "
    "Придумай ОДИН короткий сатирический абсурдный «факт» на актуальную "
    "злободневную тему (политика, мемы, интернет, общество, быт). "
    "Юмор сатирический, но без фамилий конкретных людей, без экстремизма, "
    "без оскорблений конкретных лиц и без грубой лексики. "
    "Подавай факт с полной уверенностью, как истину в последней инстанции. "
    "Одно-два предложения. Иногда коротко упомяни некоглая, сидящего на банке колы."
)

# Запасные факты, если нейросеть вдруг откажет
FALLBACK_FACTS = [
    "По официальным данным, партия, «честно» выигравшая выборы, искренне удивилась, что выборы вообще были.",
    "Эксперты выяснили: некоглай сидит на банке колы чаще, чем некоторые политики отвечают на вопросы журналистов.",
    "Статистика показала: интернет-сбор подписей собирается в 5 раз быстрее, чем ремонт дороги, обещанный к выборам.",
    "Новость дня: Мокрица снова всех удивила. Опять ничего не сделала, но удивление засчитано.",
    "Социологи подтвердили: 9 из 10 опрошенных не помнят, о чём их спрашивали в опросе. Оставшийся 1 — некоглай, и он сидит на колу.",
]

RPS_ITEMS = ["камень", "ножницы", "бумага"]
RPS_WILD = ["пистолет", "ядерная бомба", "фаллос", "банан", "некоглай с банкой колы"]
RPS_BEATS = {"камень": "ножницы", "ножницы": "бумага", "бумага": "камень"}

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

# Активные игры «угадай число»: chat_id -> загаданное число (0-10)
guess_games: dict[int, int] = {}


# --------------------------------------------------------------------------
# База данных (история переписки + некоглайметр)
# --------------------------------------------------------------------------
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id INTEGER NOT NULL, "
        "role TEXT NOT NULL, "
        "text TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS neko_counts ("
        "chat_id INTEGER PRIMARY KEY, "
        "count INTEGER NOT NULL)"
    )
    return conn


def load_history(chat_id: int) -> list:
    """Возвращает последние сообщения беседы в формате для API Gemini."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT role, text FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, MAX_HISTORY),
        ).fetchall()
    finally:
        conn.close()
    return [{"role": role, "parts": [{"text": text}]} for role, text in reversed(rows)]


def append_to_history(chat_id: int, role: str, text: str) -> None:
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO history (chat_id, role, text) VALUES (?, ?, ?)",
            (chat_id, role, text),
        )
        conn.commit()
    finally:
        conn.close()


def clear_history(chat_id: int) -> None:
    conn = _get_db()
    try:
        conn.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def add_neko_count(chat_id: int, n: int) -> None:
    """Увеличивает некоглайметр на n (персонально для чата)."""
    if n <= 0:
        return
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO neko_counts (chat_id, count) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET count = count + ?",
            (chat_id, n, n),
        )
        conn.commit()
    finally:
        conn.close()


def get_neko_count(chat_id: int) -> int:
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT count FROM neko_counts WHERE chat_id=?", (chat_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


# --------------------------------------------------------------------------
# Проверка токена
# --------------------------------------------------------------------------
if not TOKEN:
    print("=" * 60)
    print("ОШИБКА: не найден токен бота (BOT_TOKEN).")
    print()
    print("Как получить токен:")
    print("  1. В Telegram найдите @BotFather")
    print("  2. Напишите ему команду /newbot")
    print("  3. Придумайте имя бота и его username (заканчивается на 'bot')")
    print("  4. BotFather пришлёт токен вида: 123456:ABC-DEF...")
    print("  5. Создайте файл .env рядом с bot.py со строкой:")
    print("     BOT_TOKEN=ваш_токен")
    print("=" * 60)
    sys.exit(1)

if not GEMINI_API_KEY:
    print("=" * 60)
    print("ВНИМАНИЕ: не найден ключ GEMINI_API_KEY — режим нейросети отключён.")
    print()
    print("Как получить бесплатный ключ:")
    print("  1. Зайдите на https://aistudio.google.com/apikey (нужен Google-аккаунт)")
    print("  2. Нажмите 'Create API key' и скопируйте ключ")
    print("  3. Добавьте в .env строку:")
    print("     GEMINI_API_KEY=ваш_ключ")
    print("=" * 60)


# --------------------------------------------------------------------------
# Запрос к нейросети (общая функция для ответов и фактов)
# --------------------------------------------------------------------------
async def ask_gemini(contents: list, system_prompt: str) -> tuple:
    """Спрашивает Gemini (с автопереключением моделей при перегрузке).

    Возвращает: (текст_ответа, None) или (None, текст_ошибки).
    """
    models = []
    for model in [GEMINI_MODEL, *FALLBACK_MODELS]:
        if model not in models:
            models.append(model)

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }

    last_error = "Неизвестная ошибка"
    for model in models:
        try:
            url = GEMINI_URL_TEMPLATE.format(model=model, key=GEMINI_API_KEY)
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, json=payload)

            # Перегрузка/лимит/нет модели — пробуем следующую
            if response.status_code in (429, 500, 503, 404):
                last_error = f"HTTP {response.status_code} (модель {model})"
                await asyncio.sleep(3)
                continue

            response.raise_for_status()
            data = response.json()
            answer = data["candidates"][0]["content"]["parts"][0]["text"]
            return answer, None
        except (httpx.HTTPStatusError, httpx.HTTPError, KeyError, IndexError) as exc:
            last_error = str(exc)
            await asyncio.sleep(3)
            continue
    return None, last_error


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🎮 Угадай число", "✊ КНБ"],
            ["📖 Факт", "📟 Некоглайметр"],
            ["/hi", "/time"],
            ["/reset", "/help"],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши мне что-нибудь…",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие при запуске бота командой /start."""
    user = update.effective_user
    await update.message.reply_text(
        f"А, явился! Ну здравствуй, {user.first_name}. 👋🪳\n\n"
        "Я — Мокрица. Единственная. Одна я тут и живу, так что привыкай. "
        "Пиши что угодно — развлеку, если мой великий интеллект снизойдёт до тебя. "
        "А если не снизойдёт — значит, ты недостаточно интересен 😌\n\n"
        "Кстати, снизу кнопки: игры, факты и некоглайметр.",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по командам."""
    await update.message.reply_text(
        "Великая Мокрица объясняет, что здесь можно делать:\n\n"
        "💬 Просто пиши текст — отвечу, чем богат (богат я, кстати, сильно)\n"
        "🧠 Я помню нашу беседу (до 20 последних сообщений — большего ты не заслужил)\n\n"
        "Команды:\n"
        "/start — позвать меня (не приду, но отвечу)\n"
        "/help — эта инструкция, в который раз\n"
        "/hi — поздороваться, будто мы не виделись\n"
        "/time — спросить время. Да, я знаю его. Не благодари.\n"
        "/reset — стереть беседу и сделать вид, что мы не знакомы\n"
        "/guess — угадай число от 0 до 10, если дерзнёшь\n"
        "/rps — камень-ножницы-бумага (но у меня свой арсенал)\n"
        "/fact — сатирический факт дня\n"
        "/nek — некоглайметр: сколько раз я упомянула некоглая\n\n"
        "Известная фича: иногда в играх я читерю (редко, но легендарно). "
        "Это не баг — это гордость. ✨"
    )


async def hi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Попросту здороваемся в ответ."""
    await update.message.reply_text(
        f"{update.effective_user.first_name}? Опять ты. Ладно, привет. "
        "Раз уж ты поздоровался — считай, мы друзья. Учитывая мой статус, "
        "это большая честь для тебя 😏"
    )


async def time_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показываем текущее время."""
    from datetime import datetime

    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    await update.message.reply_text(
        f"Сейчас {now} ⏰ Хочешь знать, бежит ли время? "
        "Бежит. Но с такими собеседниками, как ты, оно скорее ползёт."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищаем историю переписки пользователя."""
    chat_id = update.effective_chat.id
    clear_history(chat_id)
    guess_games.pop(chat_id, None)
    await update.message.reply_text(
        "Всё, стёрто. Почистил историю так же быстро, как некоглай "
        "соскакивает с банки колы — молниеносно. Начинаем заново, "
        "с чистого листа. И в этот раз постарайся быть интереснее 🧹"
    )


# --------------------------------------------------------------------------
# Игра: угадай число (0-10)
# --------------------------------------------------------------------------
async def start_guess(update: Update) -> None:
    """Мокрица загадывает число."""
    chat_id = update.effective_chat.id
    guess_games[chat_id] = random.randint(0, 10)
    await update.message.reply_text(
        "Загадала число от 0 до 10. Всё честно, Мокрица слово даёт! "
        "Пиши число — одна попытка, больше ты не достоин 😌"
    )


async def handle_guess(update: Update, text: str) -> bool:
    """Обрабатывает ответ. Возвращает True, если игра шла."""
    chat_id = update.effective_chat.id
    target = guess_games.get(chat_id)
    if target is None:
        return False

    guess_games.pop(chat_id, None)
    user_num = int(text)

    if user_num == target:
        if random.random() < CHEAT_CHANCE:
            fake = random.randint(11, 99)  # всегда за рамками 0-10
            await update.message.reply_text(
                f"Хм?! Ну... вообще-то я загадала {fake}. Разве ты не видишь? "
                "Думай шире, выходи за рамки, как я! ✨Фича Мокрицы✨"
            )
        else:
            await update.message.reply_text(
                f"Ладно. Угадал — {target}. В этот раз без чита, "
                "считай, тебе повезло. Как некоглаю с его колой — везёт, но недолго."
            )
    else:
        await update.message.reply_text(
            f"Ха! Было {target}, а не {user_num}. Слабо. "
            "Некоглай и то угадывает быстрее, не слезая с банки колы."
        )
    return True


# --------------------------------------------------------------------------
# Игра: камень-ножницы-бумага
# --------------------------------------------------------------------------
async def rps_help(update: Update) -> None:
    """Объясняет правила КНБ."""
    await update.message.reply_text(
        "Правила простые: пиши «камень», «ножницы» или «бумагу». "
        "Я хожу классикой... но у меня в арсенале есть кое-что ещё 😏 "
        "И помни: читерство — фича, а не баг. ✨"
    )


async def play_rps(update: Update, user_choice: str) -> None:
    """Один раунд КНБ."""
    bot_choice = random.choice(RPS_ITEMS)

    if user_choice == bot_choice:
        await update.message.reply_text(
            f"{bot_choice.capitalize()} против {user_choice}. Ничья! "
            "Хоть некоглай с колы слезь — веселее бы было."
        )
        return

    if RPS_BEATS[user_choice] == bot_choice:
        # Пользователь выиграл — Мокрица может «вспомнить» про арсенал
        if random.random() < CHEAT_CHANCE:
            cheat = random.choice(RPS_WILD)
            await update.message.reply_text(
                f"Стоп-стоп. Пока ты моргал — я поменяла свой ход на {cheat}. "
                f"{cheat.capitalize()} побил твой {user_choice}. Я победила. "
                "✨Фича Мокрицы✨"
            )
        else:
            await update.message.reply_text(
                f"Ну... ты выиграл. {bot_choice} проиграл твоему {user_choice}. "
                "Ладно, признаю поражение по-королевски. Но учти: некоглай "
                "и то держался дольше."
            )
    else:
        await update.message.reply_text(
            f"{bot_choice.capitalize()} побил твой {user_choice}. Очевидно же. "
            "Я всегда права. Можешь не благодарить за урок."
        )


# --------------------------------------------------------------------------
# Факт дня (через нейросеть, на актуальную тему)
# --------------------------------------------------------------------------
async def fact_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сатирический факт, сгенерированный Gemini."""
    await update.message.chat.send_chat_action(action=ChatAction.TYPING)

    answer, error = await ask_gemini(
        [{"role": "user", "parts": [{"text": "Расскажи интересный факт."}]}],
        FACT_PROMPT,
    )
    if answer:
        # Факты тоже кормят некоглайметр
        add_neko_count(update.effective_chat.id, answer.lower().count("некогла"))
        await update.message.reply_text(f"📖 {answer}")
    else:
        await update.message.reply_text(f"📖 {random.choice(FALLBACK_FACTS)}")


# --------------------------------------------------------------------------
# Некоглайметр
# --------------------------------------------------------------------------
async def neko_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает счётчик упоминаний некоглая."""
    chat_id = update.effective_chat.id
    count = get_neko_count(chat_id)
    await update.message.reply_text(
        f"📟 Некоглайметр: за всё время я упомянула некоглая {count} раз."
        "\n\nОн так и сидит на своей банке колы, между прочим. "
        "А ты пока что позади него по количеству упоминаний."
    )


# --------------------------------------------------------------------------
# Нейросеть (обычные сообщения)
# --------------------------------------------------------------------------
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое обычное сообщение: игровые триггеры или нейросеть."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    lower = text.lower()

    if not text:
        return

    # --- Игровые кнопки/команды текстом ---
    if lower == "🎮 угадай число" or lower.startswith("угадай число"):
        await start_guess(update)
        return
    if lower == "✊ кнб" or lower.startswith("кнб"):
        await rps_help(update)
        return
    if lower == "📖 факт" or lower.startswith("факт"):
        await fact_command(update, context)
        return
    if lower == "📟 некоглайметр" or lower.startswith("некоглайметр"):
        await neko_command(update, context)
        return

    # --- Угадай число: ответ цифрой ---
    if text.isdigit() and int(text) <= 10:
        if await handle_guess(update, text):
            return

    # --- Камень-ножницы-бумага ---
    if lower in RPS_ITEMS:
        await play_rps(update, lower)
        return

    # --- Обычный режим: нейросеть ---
    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "Нейросеть ещё не подключена 😕\n"
            "Попросите администратора добавить GEMINI_API_KEY (см. /help)."
        )
        return

    await update.message.chat.send_chat_action(action=ChatAction.TYPING)

    history = load_history(chat_id)
    history.append({"role": "user", "parts": [{"text": text}]})

    answer, error = await ask_gemini(history, SYSTEM_PROMPT)

    if answer is None:
        await update.message.reply_text(
            f"Ой, что-то пошло не так с нейросетью 😕\n\n"
            f"Ошибка: {error}"
        )
        return

    # Некоглайметр пополняется из ответа
    add_neko_count(chat_id, answer.lower().count("некогла"))

    append_to_history(chat_id, "user", text)
    append_to_history(chat_id, "model", answer)

    await update.message.reply_text(answer)


# --------------------------------------------------------------------------
# Keep-alive для облачных хостингов
# --------------------------------------------------------------------------
class PingHandler(BaseHTTPRequestHandler):
    """Отвечает 'ok' на GET — так облачные хостинги видят, что сервис живой."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args) -> None:  # тихий режим, без шума в логах
        pass


def start_keepalive_server(port_from_env: str) -> None:
    """Поднимает простой HTTP-сервер в фоне (нужен для Render и подобных)."""
    port = int(port_from_env or "8080")
    server = ThreadingHTTPServer(("0.0.0.0", port), PingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Keep-alive HTTP-сервер запущен на порту {port}")


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    """Вызывается при старте: прописываем команды в меню Telegram."""
    await app.bot.set_my_commands(
        [
            ("start", "Начать работу"),
            ("help", "Справка"),
            ("guess", "Угадай число 0-10"),
            ("rps", "Камень-ножницы-бумага"),
            ("fact", "Сатирический факт"),
            ("nek", "Некоглайметр"),
            ("hi", "Поздороваться"),
            ("time", "Текущее время"),
            ("reset", "Очистить историю беседы"),
        ]
    )


def main() -> None:
    """Создаём приложение и запускаем бота."""
    if os.getenv("PORT"):
        start_keepalive_server(os.getenv("PORT"))

    builder = Application.builder().token(TOKEN)

    if PROXY:
        print(f"Использую прокси: {PROXY}")
        builder = builder.proxy(PROXY)

    builder.post_init(post_init)

    app = builder.build()

    # Группа 0: команды всегда обрабатываются первыми
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("guess", start_guess))
    app.add_handler(CommandHandler("rps", rps_help))
    app.add_handler(CommandHandler("fact", fact_command))
    app.add_handler(CommandHandler("nek", neko_command))
    app.add_handler(CommandHandler("hi", hi))
    app.add_handler(CommandHandler("time", time_now))
    app.add_handler(CommandHandler("reset", reset))

    # Группа 1: всё остальное — нейросеть и игры
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat), group=1)

    print("Бот с нейросетью запущен. Нажмите Ctrl+C, чтобы остановить.")
    app.run_polling()


if __name__ == "__main__":
    main()
