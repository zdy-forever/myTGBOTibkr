"""Remove persisted announcements and delivery history at the owner's request."""
import sqlalchemy as sa

from alembic import op

revision = "0014_ephemeral_announcements"
down_revision = "0013_user_id_whitelist"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("announcement_deliveries")
    op.drop_table("announcements")


def downgrade():
    # Restore only the schema; removed announcement history cannot be recovered.
    op.create_table("announcements",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("content", sa.Text(), nullable=False),
                    sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
                    sa.Column("important", sa.Boolean(), nullable=False, server_default="0"),
                    sa.Column("published_at", sa.DateTime(timezone=True)),
                    sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_table("announcement_deliveries",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("delivered_at", sa.DateTime(timezone=True)),
                    sa.Column("message_id", sa.BigInteger()),
                    sa.Column("pin_applied_at", sa.DateTime(timezone=True)),
                    sa.Column("pinned_at", sa.DateTime(timezone=True)),
                    sa.Column("unpinned_at", sa.DateTime(timezone=True)),
                    sa.Column("attempts", sa.Integer(), nullable=False),
                    sa.Column("last_error", sa.Text()),
                    sa.UniqueConstraint("announcement_id", "user_id", name="uq_announcement_delivery"))
