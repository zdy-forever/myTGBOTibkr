"""Persist each user's monthly trading-day schedule and retry progress."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "0002_personal_notice_day"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("notification_preferences") as batch:
        batch.add_column(sa.Column("auto_notice_day", sa.Integer(), server_default="1", nullable=False))
        batch.add_column(sa.Column("last_notice_month", sa.String(7)))
        batch.add_column(sa.Column("notice_attempt_date", sa.Date()))
        batch.add_column(sa.Column("notice_attempts", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("notice_last_attempt_at", sa.DateTime(timezone=True)))
        batch.create_check_constraint("ck_notice_day", "auto_notice_day BETWEEN 1 AND 23")
    # The previous scheduler completed the month for all subscribed users.
    # Preserve that fact so upgrading does not repeat a completed monthly push.
    months = []
    tz = ZoneInfo(os.getenv("STRATEGY_PUSH_TIMEZONE", "Asia/Shanghai"))
    for (raw,) in op.get_bind().execute(sa.text("SELECT metadata_json FROM strategy_runs")):
        payload = json.loads(raw) if isinstance(raw, str) else raw
        stamp = (payload or {}).get("monthly_notified_at")
        if stamp:
            months.append(datetime.fromisoformat(stamp).astimezone(tz).strftime("%Y-%m"))
    if months:
        op.get_bind().execute(sa.text("UPDATE notification_preferences SET last_notice_month = :month WHERE signals_enabled = true"),
                              {"month": max(months)})


def downgrade():
    with op.batch_alter_table("notification_preferences") as batch:
        batch.drop_constraint("ck_notice_day", type_="check")
        for name in ("notice_last_attempt_at", "notice_attempts", "notice_attempt_date", "last_notice_month", "auto_notice_day"):
            batch.drop_column(name)
