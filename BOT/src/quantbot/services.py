from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    CashAccount,
    InvestmentPlan,
    NotificationPreference,
    PlanFrequency,
    Position,
    Role,
    Transaction,
    TransactionType,
    User,
)
from .security import PhoneProtector


def decimal_value(value: str | int | Decimal, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
        raise ValueError("value is outside the allowed range")
    return result


MONEY_RE = re.compile(r"^(?:(?P<sym>\$|US\$)\s*)?(?P<num>\d+(?:\.\d+)?|\.\d+)\s*(?P<cur>[A-Za-z]{3}|美元|美金)?$|^(?P<cur2>[A-Za-z]{3})\s*(?P<num2>\d+(?:\.\d+)?)$")
MAX_CASH = Decimal("100000000000")


def parse_money_text(text: str) -> tuple[Decimal, str]:
    """Forgiving cash input -> (amount, currency): `200 USD`, `200usd`, `$200`, `1,000.5 usd`, `USD 200`, `200美元`; no currency means USD. Raises ValueError for anything else."""
    cleaned = text.strip().replace(",", "").replace("，", "").replace("＄", "$")
    match = MONEY_RE.match(cleaned)
    if not match:
        raise ValueError("cannot read the amount")
    amount = Decimal(match.group("num") or match.group("num2"))
    currency = match.group("cur") or match.group("cur2") or "USD"
    currency = "USD" if currency in {"美元", "美金"} else currency.upper()
    if amount > MAX_CASH:
        raise ValueError("amount is too large")
    return amount, currency


def normalize_symbol(value: str) -> str:
    value = value.strip().upper()
    if not value or len(value) > 24 or not all(c.isalnum() or c in ".-" for c in value):
        raise ValueError("invalid stock symbol")
    if value == "CURRENT_CASH":
        raise ValueError("current_cash is reserved")
    return value


def normalize_currency(value: str) -> str:
    value = value.strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ValueError("currency must be a 3-letter code")
    return value


def normalize_exchange(value: str) -> str:
    value = value.strip().upper()
    if not value or len(value) > 16 or not value.replace("_", "").isalnum():
        raise ValueError("invalid exchange")
    return value


@dataclass(frozen=True)
class RegistrationResult:
    user: User
    created: bool


class AccountService:
    def __init__(self, protector: PhoneProtector, maintainers: frozenset[int] = frozenset()) -> None:
        self.protector = protector
        self.maintainers = maintainers

    def by_telegram(self, session: Session, telegram_id: int) -> User | None:
        return session.scalar(select(User).where(User.telegram_id == telegram_id, User.is_active))

    def unregister(self, session: Session, telegram_id: int) -> bool:
        """The user's own request: delete the account and everything that belongs to it (the database cascades to positions, cash, plans, history, signals and preferences)."""
        user = self.by_telegram(session, telegram_id)
        if user is None:
            return False
        session.execute(delete(User).where(User.id == user.id))
        return True

    def register(self, session: Session, telegram_id: int, phone: str) -> RegistrationResult:
        existing = self.by_telegram(session, telegram_id)
        if existing:
            return RegistrationResult(existing, False)
        fingerprint = self.protector.fingerprint(phone)
        if session.scalar(select(User).where(User.phone_fingerprint == fingerprint)):
            raise ValueError("phone is already bound; contact an administrator for recovery")
        user = User(
            telegram_id=telegram_id,
            phone_fingerprint=fingerprint,
            phone_encrypted=self.protector.encrypt(phone),
            role=Role.MAINTAINER if telegram_id in self.maintainers else Role.USER,
        )
        session.add(user)
        session.flush()
        session.add_all([CashAccount(user_id=user.id), NotificationPreference(user_id=user.id)])
        session.flush()
        return RegistrationResult(user, True)

    def require(self, session: Session, telegram_id: int) -> User:
        user = self.by_telegram(session, telegram_id)
        if not user:
            raise PermissionError("registration required")
        return user

    def require_maintainer(self, session: Session, telegram_id: int) -> User:
        user = self.require(session, telegram_id)
        if telegram_id not in self.maintainers:
            raise PermissionError("maintainer access required")
        return user


class PortfolioService:
    @staticmethod
    def list_positions(session: Session, user_id: str) -> list[Position]:
        query = select(Position).where(Position.user_id == user_id).order_by(Position.symbol)
        return list(session.scalars(query))

    @staticmethod
    def get_position(session: Session, user_id: str, symbol: str, exchange: str = "US") -> Position | None:
        return session.scalar(select(Position).where(
            Position.user_id == user_id,
            Position.symbol == normalize_symbol(symbol),
            Position.exchange == normalize_exchange(exchange),
        ))

    def add_position(self, session: Session, user: User, symbol: str, shares, average_cost,
                     currency: str = "USD", exchange: str = "US", *, actor: int) -> Position:
        position = Position(
            user_id=user.id, symbol=normalize_symbol(symbol), exchange=normalize_exchange(exchange),
            shares=decimal_value(shares, positive=True), average_cost=decimal_value(average_cost),
            currency=normalize_currency(currency),
        )
        session.add(position)
        try:
            session.flush()
        except IntegrityError as exc:
            raise ValueError("position already exists") from exc
        self._audit(session, user.id, TransactionType.POSITION_CREATE, actor, position.symbol,
                    position.exchange, None, self._snapshot(position))
        return position

    def update_position(self, session: Session, user: User, symbol: str, *, actor: int,
                        shares=None, average_cost=None, currency=None, exchange: str = "US") -> Position:
        position = self.get_position(session, user.id, symbol, exchange)
        if not position:
            raise LookupError("position not found")
        before = self._snapshot(position)
        if shares is not None:
            position.shares = decimal_value(shares, positive=True)
        if average_cost is not None:
            position.average_cost = decimal_value(average_cost)
        if currency is not None:
            position.currency = normalize_currency(currency)
        self._audit(session, user.id, TransactionType.POSITION_UPDATE, actor, position.symbol,
                    position.exchange, before, self._snapshot(position))
        return position

    def remove_position(self, session: Session, user: User, symbol: str, exchange: str = "US", *, actor: int) -> None:
        position = self.get_position(session, user.id, symbol, exchange)
        if not position:
            raise LookupError("position not found")
        before = self._snapshot(position)
        session.delete(position)
        self._audit(session, user.id, TransactionType.POSITION_DELETE, actor,
                    position.symbol, position.exchange, before, None)

    @staticmethod
    def cash(session: Session, user_id: str) -> CashAccount:
        cash = session.get(CashAccount, user_id)
        if not cash:
            raise LookupError("cash account not found")
        return cash

    def set_cash(self, session: Session, user: User, balance, currency: str, *, actor: int) -> CashAccount:
        cash = self.cash(session, user.id)
        before = {"balance": str(cash.balance), "currency": cash.currency}
        cash.balance = decimal_value(balance)
        cash.currency = normalize_currency(currency)
        self._audit(session, user.id, TransactionType.CASH_SET, actor, None, None, before,
                    {"balance": str(cash.balance), "currency": cash.currency})
        return cash

    @staticmethod
    def _snapshot(position: Position) -> dict:
        return {"symbol": position.symbol, "exchange": position.exchange,
                "shares": str(position.shares), "average_cost": str(position.average_cost),
                "currency": position.currency}

    @staticmethod
    def _audit(session: Session, user_id: str, kind: TransactionType, actor: int,
               symbol: str | None, exchange: str | None, before: dict | None, after: dict | None,
               idempotency_key: str | None = None) -> None:
        session.add(Transaction(user_id=user_id, type=kind, symbol=symbol, exchange=exchange,
                                before_data=before, after_data=after,
                                actor_telegram_id=actor, idempotency_key=idempotency_key))


MODE_TO_FREQUENCY = {
    0: PlanFrequency.MANUAL, 1: PlanFrequency.MONTHLY, 2: PlanFrequency.WEEKLY,
    3: PlanFrequency.QUARTERLY, 4: PlanFrequency.YEARLY,
}
FREQUENCY_TO_MODE = {value: key for key, value in MODE_TO_FREQUENCY.items()}


def _advance(dt: datetime, frequency: PlanFrequency) -> datetime:
    if frequency == PlanFrequency.WEEKLY:
        return dt + timedelta(days=7)
    months = {PlanFrequency.MONTHLY: 1, PlanFrequency.QUARTERLY: 3, PlanFrequency.YEARLY: 12}.get(frequency)
    if not months:
        raise ValueError("manual plans do not execute")
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


class PlanService:
    def set_plan(self, session: Session, user: User, amount, currency: str,
                 frequency: PlanFrequency, next_execution_at: datetime) -> InvestmentPlan:
        if frequency == PlanFrequency.MANUAL:
            raise ValueError("manual mode does not create an investment plan")
        if next_execution_at.tzinfo is None:
            raise ValueError("next execution time must include a timezone")
        next_execution_at = next_execution_at.astimezone(UTC)
        cash = session.get(CashAccount, user.id)
        normalized_currency = normalize_currency(currency)
        if normalized_currency != cash.currency:
            raise ValueError("investment plan currency must match the cash account currency")
        plan = session.scalar(select(InvestmentPlan).where(InvestmentPlan.user_id == user.id))
        values = {
            "amount": decimal_value(amount, positive=True),
            "currency": normalized_currency,
            "frequency": frequency,
            "next_execution_at": next_execution_at,
            "enabled": True,
        }
        if plan:
            for key, value in values.items():
                setattr(plan, key, value)
        else:
            plan = InvestmentPlan(user_id=user.id, **values)
            session.add(plan)
        cash.management_mode = frequency
        return plan

    def process_due(self, session: Session, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        plans = session.scalars(select(InvestmentPlan).where(
            InvestmentPlan.enabled, InvestmentPlan.next_execution_at <= now
        )).all()
        count = 0
        for plan in plans:
            due = plan.next_execution_at
            key = f"plan:{plan.id}:{due.isoformat()}"
            if session.scalar(select(Transaction.id).where(Transaction.idempotency_key == key)):
                plan.next_execution_at = _advance(due, plan.frequency)
                continue
            cash = session.get(CashAccount, plan.user_id)
            before = str(cash.balance)
            cash.balance += plan.amount
            PortfolioService._audit(
                session, plan.user_id, TransactionType.PLAN_CONTRIBUTION, 0, None, None,
                {"balance": before}, {"balance": str(cash.balance), "amount": str(plan.amount)}, key,
            )
            plan.next_execution_at = _advance(due, plan.frequency)
            count += 1
        return count


def system_stats(session: Session) -> dict[str, int]:
    return {
        "users": session.scalar(select(func.count()).select_from(User)) or 0,
        "positions": session.scalar(select(func.count()).select_from(Position)) or 0,
        "transactions": session.scalar(select(func.count()).select_from(Transaction)) or 0,
    }
