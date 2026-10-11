"""Add fingerprint whitelist and durable Telegram Stars subscriptions."""
from alembic import op
import sqlalchemy as sa

revision = "0004_premium_access"
down_revision = "0003_user_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "phone_whitelist",
        sa.Column("phone_fingerprint", sa.String(64), primary_key=True),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "premium_subscriptions",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "subscription_payments",
        sa.Column("telegram_charge_id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_stars", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscription_payments_user_id", "subscription_payments", ["user_id"])


def downgrade():
    op.drop_table("subscription_payments")
    op.drop_table("premium_subscriptions")
    op.drop_table("phone_whitelist")
