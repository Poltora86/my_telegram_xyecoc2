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


async def post_init(app: Application) -> None:
    """Вызывается при старте: прописываем команды в меню Telegram."""
    await app.bot.set_my_commands(
        [
            ("start", "Начать работу"),
            ("help", "Справка"),
            ("hi", "Поздороваться"),
            ("time", "Текущее время"),
            ("reset", "Очистить историю беседы"),
        ]
    )


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

    builder.post_init(post_init)

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
