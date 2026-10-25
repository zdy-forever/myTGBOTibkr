"""Owner-only S2 paper ledger backed by Alpaca paper fills."""
import sqlalchemy as sa

from alembic import op

revision = "0018_paper_trading"
down_revision = "0017_aud_cash"
branch_labels = depends_on = None


def upgrade():
    op.create_table("paper_accounts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("cash_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("monthly_aud", sa.Numeric(20, 8), nullable=False),
        sa.Column("holdings", sa.JSON(), nullable=False),
        sa.Column("benchmark_cash_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("benchmark_shares", sa.Numeric(24, 10), nullable=False),
        sa.Column("units", sa.Numeric(24, 10), nullable=False),
        sa.Column("benchmark_units", sa.Numeric(24, 10), nullable=False),
        sa.Column("deposits_aud", sa.Numeric(20, 8), nullable=False),
        sa.Column("deposits_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("fees_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("dividends_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("peak_nav_per_unit", sa.Numeric(24, 10), nullable=False),
        sa.Column("max_drawdown", sa.Numeric(12, 8), nullable=False),
        sa.Column("benchmark_peak", sa.Numeric(24, 10), nullable=False),
        sa.Column("benchmark_max_drawdown", sa.Numeric(12, 8), nullable=False),
        sa.Column("last_session", sa.Date()),
        sa.Column("last_valued_on", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("paper_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.String(32), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "key", name="uq_paper_event_key"))
    op.create_index("ix_paper_events_account_id", "paper_events", ["account_id"])


def downgrade():
    op.drop_table("paper_events")
    op.drop_table("paper_accounts")
