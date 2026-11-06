import logging
import sqlite3

from config import validate_config


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        validate_config()
        from bot import bot
        from database import init_db

        init_db()
    except ValueError as error:
        logging.error("%s", error)
        return 1
    except sqlite3.Error as error:
        logging.error("Не удалось открыть базу: %s", type(error).__name__)
        return 1
    logging.info("Бот запущен. Для остановки нажми Ctrl+C.")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=20)
    finally:
        bot.stop_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
