from decimal import Decimal

import pytest
from sqlalchemy import select

from quantbot.db import transaction
from quantbot.models import Position, Transaction
from quantbot.services import PortfolioService


def register(factory, accounts, telegram_id=1):
    with transaction(factory) as session:
        return accounts.register(session, telegram_id, f"+6140000{telegram_id:04d}").user.id


def test_position_crud_decimal_and_audit(factory, accounts):
    user_id = register(factory, accounts)
    service = PortfolioService()
    with transaction(factory) as session:
        user = accounts.require(session, 1)
        item = service.add_position(session, user, "isrg", "0.5500000001", "374.21", actor=1)
        assert item.shares == Decimal("0.5500000001")
        service.update_position(session, user, "ISRG", shares="0.75", average_cost="380.12345678", actor=1)
    with transaction(factory) as session:
        item = service.get_position(session, user_id, "ISRG")
        assert item.shares == Decimal("0.7500000000")
        assert item.average_cost == Decimal("380.12345678")
        service.remove_position(session, accounts.require(session, 1), "ISRG", actor=1)
    with transaction(factory) as session:
        assert session.scalar(select(Position)) is None
        audit = session.scalars(select(Transaction).where(Transaction.user_id == user_id)).all()
        assert len(audit) == 3
        assert audit[1].before_data["shares"] == "0.5500000001"


def test_user_data_isolation(factory, accounts):
    first = register(factory, accounts, 1)
    second = register(factory, accounts, 2)
    with transaction(factory) as session:
        PortfolioService().add_position(session, accounts.require(session, 1), "AAPL", 1, 100, actor=1)
    with transaction(factory) as session:
        assert len(PortfolioService.list_positions(session, first)) == 1
        assert PortfolioService.list_positions(session, second) == []
        assert PortfolioService.get_position(session, second, "AAPL") is None


def test_transaction_rolls_back_on_error(factory, accounts):
    register(factory, accounts, 1)
    with pytest.raises(RuntimeError), transaction(factory) as session:
        PortfolioService().add_position(
            session, accounts.require(session, 1), "AAPL", 1, 100, actor=1
        )
        raise RuntimeError("telegram-style downstream failure")
    with transaction(factory) as session:
        assert session.scalar(select(Position)) is None


@pytest.mark.parametrize("field,value", [
    ("shares", "1e309"), ("shares", "0.00000000001"),
    ("average_cost", "1e309"), ("average_cost", "1.000000001"),
])
def test_position_rejects_values_sqlite_cannot_safely_store(factory, accounts, field, value):
    register(factory, accounts)
    values = {"shares": "1", "average_cost": "100", field: value}
    with pytest.raises(ValueError), transaction(factory) as session:
        PortfolioService().add_position(session, accounts.require(session, 1), "AAPL",
                                        values["shares"], values["average_cost"], actor=1)
    with transaction(factory) as session:
        assert session.scalar(select(Position)) is None
