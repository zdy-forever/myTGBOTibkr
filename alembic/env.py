from logging.config import fileConfig
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.abspath("src"))
from quantbot.db import Base  # noqa: E402
from quantbot import models  # noqa: E402,F401
from quantbot.config import _load_dotenv  # noqa: E402

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
_load_dotenv()
if os.getenv("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
