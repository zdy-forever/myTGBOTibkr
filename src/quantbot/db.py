from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_db_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        path = Path(url.removeprefix("sqlite:///"))
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if url != "sqlite:///:memory:":
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        with session.begin():
            yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
