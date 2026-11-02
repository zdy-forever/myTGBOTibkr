"""Admin-managed email reminders: mailbox -> bot user + strategy list."""
import sqlalchemy as sa

from alembic import op

revision = "0019_email_subscriptions"
down_revision = "0018_paper_trading"
branch_labels = depends_on = None


def upgrade():
    op.add_column("daily_reports", sa.Column("has_actions", sa.Boolean(), nullable=True))
    op.create_table("email_subscriptions",
        sa.Column("email", sa.String(254), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategies", sa.String(64), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_email_subscriptions_user_id", "email_subscriptions", ["user_id"])
    op.create_table("email_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(254), sa.ForeignKey("email_subscriptions.email", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.String(32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("email", "strategy_id", "session_date", name="uq_email_delivery"))
    op.create_index("ix_email_deliveries_email", "email_deliveries", ["email"])


def downgrade():
    op.drop_index("ix_email_deliveries_email", "email_deliveries")
    op.drop_table("email_deliveries")
    op.drop_index("ix_email_subscriptions_user_id", "email_subscriptions")
    op.drop_table("email_subscriptions")
    with op.batch_alter_table("daily_reports") as batch:
        batch.drop_column("has_actions")
