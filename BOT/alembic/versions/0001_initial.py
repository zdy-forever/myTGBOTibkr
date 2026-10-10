"""Initial multi-user portfolio schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

role = sa.Enum("USER", "MAINTAINER", name="role")
frequency = sa.Enum("MANUAL", "MONTHLY", "WEEKLY", "QUARTERLY", "YEARLY", name="planfrequency")
tx_type = sa.Enum("POSITION_CREATE", "POSITION_UPDATE", "POSITION_DELETE", "CASH_SET", "CASH_ADJUST", "PLAN_CONTRIBUTION", "CSV_IMPORT", name="transactiontype")
action = sa.Enum("BUY", "HOLD", "SELL", name="signalaction")
run_status = sa.Enum("RUNNING", "COMPLETED", "FAILED", "SKIPPED", name="runstatus")


def upgrade() -> None:
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("phone_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("phone_encrypted", sa.Text), sa.Column("role", role, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.create_index("ix_users_phone_fingerprint", "users", ["phone_fingerprint"], unique=True)
    op.create_table("cash_accounts", sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("balance", sa.Numeric(20, 8), nullable=False), sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("management_mode", frequency, nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("investment_plans", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False), sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("frequency", frequency, nullable=False), sa.Column("next_execution_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("positions", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(24), nullable=False), sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("shares", sa.Numeric(24, 10), nullable=False), sa.Column("average_cost", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "symbol", "exchange", name="uq_position_security"))
    op.create_index("ix_positions_user_id", "positions", ["user_id"])
    op.create_table("transactions", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", tx_type, nullable=False), sa.Column("symbol", sa.String(24)), sa.Column("exchange", sa.String(16)),
        sa.Column("before_data", sa.JSON), sa.Column("after_data", sa.JSON), sa.Column("actor_telegram_id", sa.BigInteger, nullable=False),
        sa.Column("idempotency_key", sa.String(160), unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"])
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_table("notification_preferences", sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("signals_enabled", sa.Boolean, nullable=False), sa.Column("announcements_enabled", sa.Boolean, nullable=False),
        sa.Column("quiet_start_hour", sa.Integer), sa.Column("quiet_end_hour", sa.Integer), sa.Column("timezone", sa.String(64), nullable=False))
    op.create_table("strategy_runs", sa.Column("id", sa.Integer, primary_key=True), sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("strategy_version", sa.String(80), nullable=False), sa.Column("status", run_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text), sa.Column("metadata_json", sa.JSON),
        sa.UniqueConstraint("trading_date", "strategy_version", name="uq_strategy_daily_run"))
    op.create_table("signals", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("strategy_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(24), nullable=False), sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("action", action, nullable=False), sa.Column("reference_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("price_time", sa.DateTime(timezone=True), nullable=False), sa.Column("p_score", sa.Numeric(12, 6)),
        sa.Column("f_score", sa.Numeric(12, 6)), sa.Column("v_score", sa.Numeric(12, 6)),
        sa.Column("buy_low", sa.Numeric(20, 8)), sa.Column("buy_high", sa.Numeric(20, 8)),
        sa.Column("sell_low", sa.Numeric(20, 8)), sa.Column("sell_high", sa.Numeric(20, 8)),
        sa.Column("strength", sa.Numeric(8, 6)), sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("strategy_version", sa.String(80), nullable=False), sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "symbol", "exchange", name="uq_signal_run_security"))
    op.create_index("ix_signals_run_id", "signals", ["run_id"])
    op.create_table("user_signals", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_id", sa.Integer, sa.ForeignKey("signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", action, nullable=False), sa.Column("recommendation", sa.Text, nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True)), sa.Column("notification_attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.Text), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "signal_id", name="uq_user_signal"))
    op.create_index("ix_user_signals_user_id", "user_signals", ["user_id"])
    op.create_index("ix_user_signals_signal_id", "user_signals", ["signal_id"])
    op.create_index("ix_user_signal_pending", "user_signals", ["notified_at", "notification_attempts"])
    op.create_table("announcements", sa.Column("id", sa.Integer, primary_key=True), sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger, nullable=False), sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("announcement_deliveries", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("announcement_id", sa.Integer, sa.ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)), sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.Text), sa.UniqueConstraint("announcement_id", "user_id", name="uq_announcement_delivery"))


def downgrade() -> None:
    for table in ["announcement_deliveries", "announcements", "user_signals", "signals", "strategy_runs",
                  "notification_preferences", "transactions", "positions", "investment_plans", "cash_accounts", "users"]:
        op.drop_table(table)
