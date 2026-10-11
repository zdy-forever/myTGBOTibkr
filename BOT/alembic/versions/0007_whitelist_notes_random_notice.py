"""Add private whitelist labels and the random monthly notice mode."""
from alembic import op
import sqlalchemy as sa

revision = "0007_whitelist_notes_random_notice"
down_revision = "0006_admin_phone_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("phone_whitelist", sa.Column("label", sa.String(80), nullable=True))
    op.add_column("notification_preferences", sa.Column("random_notice_started_on", sa.Date(), nullable=True))
    with op.batch_alter_table("notification_preferences") as batch:
        batch.drop_constraint("ck_notice_day", type_="check")
        batch.create_check_constraint("ck_notice_day", "auto_notice_day BETWEEN 0 AND 23")


def downgrade():
    # Random-mode users return to the original first-trading-day default.
    op.execute("UPDATE notification_preferences SET auto_notice_day = 1 WHERE auto_notice_day = 0")
    with op.batch_alter_table("notification_preferences") as batch:
        batch.drop_constraint("ck_notice_day", type_="check")
        batch.create_check_constraint("ck_notice_day", "auto_notice_day BETWEEN 1 AND 23")
    op.drop_column("notification_preferences", "random_notice_started_on")
    op.drop_column("phone_whitelist", "label")
