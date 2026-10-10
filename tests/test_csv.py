from decimal import Decimal

import pytest

from quantbot.csv_io import export_portfolio_csv, import_portfolio_csv
from quantbot.db import transaction
from quantbot.models import PlanFrequency
from quantbot.services import PortfolioService

CSV = """Company,Shares Held,Average Price
current_cash,2,1000 USD
ISRG,0.55,374.21 USD
"""


def test_csv_cash_special_row_round_trip(factory, accounts):
    with transaction(factory) as session:
        user = accounts.register(session, 1, "+61400000001").user
        result = import_portfolio_csv(session, user, CSV, actor=1)
        assert result.positions_created == 1
        assert result.cash_updated
    with transaction(factory) as session:
        user = accounts.require(session, 1)
        cash = PortfolioService.cash(session, user.id)
        assert cash.balance == Decimal("1000.00000000")
        assert cash.management_mode == PlanFrequency.WEEKLY
        assert "current_cash,2,1000.00000000 USD" in export_portfolio_csv(session, user)


def test_csv_is_atomic_on_invalid_late_row(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
    bad = CSV + "BAD,not-a-number,10 USD\n"
    with pytest.raises(ValueError), transaction(factory) as session:
        import_portfolio_csv(session, accounts.require(session, 1), bad, actor=1)
    with transaction(factory) as session:
        user = accounts.require(session, 1)
        assert PortfolioService.list_positions(session, user.id) == []
        assert PortfolioService.cash(session, user.id).balance == 0
