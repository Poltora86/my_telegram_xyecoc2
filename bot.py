"""
Telegram-бот с нейросетью (Google Gemini).

Команды:
    /start, /help  — руководство
    /hi            — просто привет
    /time          — текущее время
    /reset         — стереть историю переписки с нейросетью

Любое другое текстовое сообщение пересылается в Google Gemini,
и её ответ приходит пользователю. У каждого собеседника своя история,
которая хранится в базе SQLite (файл chat_history.db) и переживает
перезапуски бота.

Как запустить локально:
    1. В файле .env укажите BOT_TOKEN и GEMINI_API_KEY (см. .env.example)
    2. .venv/Scripts/activate
    3. python bot.py
"""

import logging
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from dotenv import load_dotenv
from telegram import Update
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.db")
MAX_HISTORY = 20  # сколько последних сообщений помнить на собеседника

SYSTEM_PROMPT = (
    "Ты дружелюбный ассистент, живущий в Telegram. "
    "Отвечай кратко, по-русски, с юмором, но по делу. "
    "Если вопрос вне твоих знаний — честно скажи об этом."
)

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

# --------------------------------------------------------------------------
# База данных (история переписки)
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
# Команды
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие при запуске бота командой /start."""
    user = update.effective_user
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        "Я бот с нейросетью Google Gemini.\n\n"
        "Пиши мне обычные сообщения — я отвечу умно 😉\n"
        "Команды:\n"
        "/start — это сообщение\n"
        "/help — справка\n"
        "/hi — поздороваться\n"
        "/time — текущее время\n"
        "/reset — стереть историю беседы\n"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по командам."""
    await update.message.reply_text(
        "Что я умею:\n"
        "💬 Просто пиши мне текст — отвечает нейросеть Gemini\n"
        "🧠 Я помню контекст беседы (до 20 последних сообщений)\n\n"
        "Команды:\n"
        "/start — начать работу\n"
        "/help — эта справка\n"
        "/hi — просто привет\n"
        "/time — показать текущее время\n"
        "/reset — начать новую беседу (очистить историю)\n"
    )


async def hi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Попросту здороваемся в ответ."""
    await update.message.reply_text(f"Привет, {update.effective_user.first_name}! Рад тебя видеть 😊")


async def time_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показываем текущее время."""
    from datetime import datetime

    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    await update.message.reply_text(f"Сейчас {now} ⏰")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищаем историю переписки пользователя."""
    chat_id = update.effective_chat.id
    clear_history(chat_id)
    await update.message.reply_text("История очищена. Начнём с чистого листа 🧹")


# --------------------------------------------------------------------------
# Нейросеть
# --------------------------------------------------------------------------
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое обычное сообщение уходит в Gemini."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if not text:
        return

    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "Нейросеть ещё не подключена 😕\n"
            "Попросите администратора добавить GEMINI_API_KEY (см. /help)."
        )
        return

    # Показываем «печатает…»
    await update.message.chat.send_chat_action(action=ChatAction.TYPING)

    # История из базы + новое сообщение пользователя
    history = load_history(chat_id)
    history.append({"role": "user", "parts": [{"text": text}]})

    try:
        payload = {
            "contents": history,
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        }
        url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        answer = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        await update.message.reply_text(
            f"Ой, что-то пошло не так с нейросетью 😕\n\n"
            f"Ошибка: {exc}"
        )
        return

    # Сохраняем оба сообщения в базу
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
def main() -> None:
    """Создаём приложение и запускаем бота."""
    if os.getenv("PORT"):
        start_keepalive_server(os.getenv("PORT"))

    builder = Application.builder().token(TOKEN)

    if PROXY:
        print(f"Использую прокси: {PROXY}")
        builder = builder.proxy(PROXY)

    app = builder.build()

    # Группа 0: команды всегда обрабатываются первыми
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hi", hi))
    app.add_handler(CommandHandler("time", time_now))
    app.add_handler(CommandHandler("reset", reset))

    # Группа 1: всё остальное — нейросети
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat), group=1)

    print("Бот с нейросетью запущен. Нажмите Ctrl+C, чтобы остановить.")
    app.run_polling()


if __name__ == "__main__":
    main()