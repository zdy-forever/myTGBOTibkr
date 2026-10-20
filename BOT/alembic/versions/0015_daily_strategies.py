"""Daily reports, private stock pools and explicit strategy cash."""
from alembic import op
import sqlalchemy as sa

revision = "0015_daily_strategies"
down_revision = "0014_ephemeral_announcements"
branch_labels = depends_on = None


def upgrade():
    op.create_table("strategy_cash",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("strategy_id", sa.String(32), primary_key=True),
        sa.Column("balance", sa.Numeric(20, 8), nullable=False),
        sa.CheckConstraint("balance >= 0", name="ck_strategy_cash_balance"))
    op.create_table("strategy_pools",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("symbols", sa.JSON(), nullable=False))
    op.create_table("strategy_snapshots",
        sa.Column("strategy_id", sa.String(32), primary_key=True),
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False))
    op.create_table("daily_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.String(32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data_ready", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("user_id", "strategy_id", "session_date", name="uq_daily_report"))
    op.create_index("ix_daily_reports_user_id", "daily_reports", ["user_id"])


def downgrade():
    for table in ("daily_reports", "strategy_snapshots", "strategy_pools", "strategy_cash"):
        op.drop_table(table)
