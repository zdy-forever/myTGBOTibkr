"""Store user/maintainer messages for the 14-day retention window."""
from alembic import op
import sqlalchemy as sa

revision = "0003_user_chat"
down_revision = "0002_personal_notice_day"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replied_by_telegram_id", sa.BigInteger()),
        sa.Column("reply_body", sa.Text()),
        sa.Column("replied_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_chat_messages_user_id", "chat_messages", ["user_id"])
    op.create_table(
        "chat_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("maintainer_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("message_id", "maintainer_telegram_id", name="uq_chat_delivery"),
    )
    op.create_index("ix_chat_deliveries_message_id", "chat_deliveries", ["message_id"])


def downgrade():
    op.drop_table("chat_deliveries")
    op.drop_table("chat_messages")
