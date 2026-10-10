from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Small dependency-free .env loader; existing environment always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("'"))


def _ids(value: str) -> frozenset[int]:
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_url: str = "sqlite:///data/quantbot.db"
    phone_hmac_secret: str = ""
    phone_encryption_key: str | None = None
    maintainer_telegram_ids: frozenset[int] = field(default_factory=frozenset)
    log_level: str = "INFO"
    backup_dir: Path = Path("backups")
    backup_retention_days: int = 30
    strategy_autorun: bool = False
    strategy_push_timezone: str = "Asia/Shanghai"
    strategy_push_hour: int = 10

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> Settings:
        _load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        secret = os.getenv("PHONE_HMAC_SECRET", "")
        if require_token and not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if len(secret) < 32:
            raise ValueError("PHONE_HMAC_SECRET must contain at least 32 characters")
        return cls(
            telegram_bot_token=token,
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/quantbot.db"),
            phone_hmac_secret=secret,
            phone_encryption_key=os.getenv("PHONE_ENCRYPTION_KEY") or None,
            maintainer_telegram_ids=_ids(os.getenv("MAINTAINER_TELEGRAM_IDS", "")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            backup_dir=Path(os.getenv("BACKUP_DIR", "backups")),
            backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
            strategy_autorun=os.getenv("STRATEGY_AUTORUN", "0").strip().lower() in {"1", "true", "yes"},
            strategy_push_timezone=os.getenv("STRATEGY_PUSH_TIMEZONE", "Asia/Shanghai"),
            strategy_push_hour=int(os.getenv("STRATEGY_PUSH_HOUR", "10")),
        )
