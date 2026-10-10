from __future__ import annotations

import pytest

from quantbot.db import Base, create_db_engine, session_factory
from quantbot.security import PhoneProtector
from quantbot.services import AccountService


@pytest.fixture
def factory(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield session_factory(engine)
    engine.dispose()


@pytest.fixture
def accounts():
    return AccountService(PhoneProtector("x" * 32), frozenset({999}))
