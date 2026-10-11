"""Track important announcement delivery and per-chat pin replacement."""
import sqlalchemy as sa

from alembic import op

revision = "0008_important_announcements"
down_revision = "0007_whitelist_notes_random_notice"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("announcements", sa.Column("important", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("announcement_deliveries", sa.Column("message_id", sa.BigInteger()))
    op.add_column("announcement_deliveries", sa.Column("pin_applied_at", sa.DateTime(timezone=True)))
    op.add_column("announcement_deliveries", sa.Column("pinned_at", sa.DateTime(timezone=True)))
    op.add_column("announcement_deliveries", sa.Column("unpinned_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("announcement_deliveries", "unpinned_at")
    op.drop_column("announcement_deliveries", "pinned_at")
    op.drop_column("announcement_deliveries", "pin_applied_at")
    op.drop_column("announcement_deliveries", "message_id")
    op.drop_column("announcements", "important")
