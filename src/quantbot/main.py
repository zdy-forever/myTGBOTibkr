from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from telegram.ext import Application

from .bot import configure_application
from .config import Settings
from .db import create_db_engine, session_factory
from .security import PhoneProtector
from .services import AccountService


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


def build_application(settings: Settings) -> Application:
    engine = create_db_engine(settings.database_url)
    factory = session_factory(engine)
    accounts = AccountService(
        PhoneProtector(settings.phone_hmac_secret, settings.phone_encryption_key),
        settings.maintainer_telegram_ids,
    )
    application = Application.builder().token(settings.telegram_bot_token).build()
    return configure_application(application, accounts, factory)


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    build_application(settings).run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
