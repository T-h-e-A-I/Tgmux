"""Entry point: python -m orchestrator"""

import logging
import sys

from telegram import Update
from telegram.ext import Application

from . import bot, config


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not config.BOT_TOKEN or not config.OWNER_ID:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID in .env (see .env.example)")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(bot.post_init)
        .build()
    )
    bot.register(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
