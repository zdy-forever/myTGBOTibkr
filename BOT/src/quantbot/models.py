from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Role(str, enum.Enum):
    USER = "user"
    MAINTAINER = "maintainer"


class PlanFrequency(str, enum.Enum):
    MANUAL = "manual"
    MONTHLY = "monthly"
    WEEKLY = "weekly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class TransactionType(str, enum.Enum):
    POSITION_CREATE = "position_create"
    POSITION_UPDATE = "position_update"
    POSITION_DELETE = "position_delete"
    CASH_SET = "cash_set"
    CASH_ADJUST = "cash_adjust"
    PLAN_CONTRIBUTION = "plan_contribution"
    CSV_IMPORT = "csv_import"


class SignalAction(str, enum.Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    phone_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    phone_encrypted: Mapped[str | None] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.USER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    positions: Mapped[list[Position]] = relationship(back_populates="user")


class UserStrategy(Base):
    __tablename__ = "user_strategies"
    __table_args__ = (CheckConstraint("weight_percent > 0 AND weight_percent <= 100", name="ck_strategy_weight"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    weight_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=Decimal(100))
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StrategyCash(Base):
    __tablename__ = "strategy_cash"
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_strategy_cash_balance"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)


class StrategyPool(Base):
    __tablename__ = "strategy_pools"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False)


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("user_id", "strategy_id", "session_date", name="uq_daily_report"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data_ready: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class StrategySnapshot(Base):
    __tablename__ = "strategy_snapshots"
    strategy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class AdminGrant(Base):
    __tablename__ = "admin_grants"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(80))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AdminPhoneGrant(Base):
    __tablename__ = "admin_phone_grants"
    phone_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Claimed by the first verified account; a recycled number cannot silently
    # transfer administrator rights to a different Telegram account.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    label: Mapped[str | None] = mapped_column(String(80))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AdminBootstrap(Base):
    __tablename__ = "admin_bootstrap"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PhoneWhitelist(Base):
    __tablename__ = "phone_whitelist"
    phone_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(80))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserIdWhitelist(Base):
    __tablename__ = "user_id_whitelist"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    label: Mapped[str | None] = mapped_column(String(80))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PremiumSubscription(Base):
    __tablename__ = "premium_subscriptions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SubscriptionPayment(Base):
    __tablename__ = "subscription_payments"
    telegram_charge_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_stars: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CashAccount(Base):
    __tablename__ = "cash_accounts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    management_mode: Mapped[PlanFrequency] = mapped_column(Enum(PlanFrequency), default=PlanFrequency.MANUAL)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InvestmentPlan(Base):
    __tablename__ = "investment_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    frequency: Mapped[PlanFrequency] = mapped_column(Enum(PlanFrequency), nullable=False)
    next_execution_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("user_id", "symbol", "exchange", name="uq_position_security"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), default="US", nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    user: Mapped[User] = relationship(back_populates="positions")


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(24))
    exchange: Mapped[str | None] = mapped_column(String(16))
    before_data: Mapped[dict | None] = mapped_column(JSON)
    after_data: Mapped[dict | None] = mapped_column(JSON)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("run_id", "symbol", "exchange", name="uq_signal_run_security"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), default="US", nullable=False)
    action: Mapped[SignalAction] = mapped_column(Enum(SignalAction), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    p_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    f_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    v_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    buy_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    buy_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    sell_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    sell_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    strength: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSignal(Base):
    __tablename__ = "user_signals"
    __table_args__ = (UniqueConstraint("user_id", "signal_id", name="uq_user_signal"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), index=True)
    action: Mapped[SignalAction] = mapped_column(Enum(SignalAction), nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    replied_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_body: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatDelivery(Base):
    __tablename__ = "chat_deliveries"
    __table_args__ = (UniqueConstraint("message_id", "maintainer_telegram_id", name="uq_chat_delivery"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True)
    maintainer_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (CheckConstraint("auto_notice_day BETWEEN 0 AND 23", name="ck_notice_day"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    signals_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    announcements_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    quiet_start_hour: Mapped[int | None] = mapped_column(Integer)
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    auto_notice_day: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    random_notice_started_on: Mapped[date | None] = mapped_column(Date)
    last_notice_month: Mapped[str | None] = mapped_column(String(7))
    notice_attempt_date: Mapped[date | None] = mapped_column(Date)
    notice_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    notice_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyRun(Base):
    __tablename__ = "strategy_runs"
    __table_args__ = (UniqueConstraint("trading_date", "strategy_version", name="uq_strategy_daily_run"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)


Index("ix_user_signal_pending", UserSignal.notified_at, UserSignal.notification_attempts)
