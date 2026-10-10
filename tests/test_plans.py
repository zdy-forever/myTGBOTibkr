from datetime import UTC, datetime
from decimal import Decimal

from quantbot.db import transaction
from quantbot.models import PlanFrequency
from quantbot.services import PlanService, PortfolioService


def test_plan_execution_is_idempotent(factory, accounts):
    due = datetime(2026, 1, 31, 10, tzinfo=UTC)
    with transaction(factory) as session:
        user = accounts.register(session, 1, "+61400000001").user
        PlanService().set_plan(session, user, "125.50", "USD", PlanFrequency.MONTHLY, due)
    with transaction(factory) as session:
        assert PlanService().process_due(session, datetime(2026, 2, 1, tzinfo=UTC)) == 1
    with transaction(factory) as session:
        assert PlanService().process_due(session, datetime(2026, 2, 1, tzinfo=UTC)) == 0
        user = accounts.require(session, 1)
        assert PortfolioService.cash(session, user.id).balance == Decimal("125.50000000")
