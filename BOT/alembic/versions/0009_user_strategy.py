"""Persist each user's selected strategy; existing accounts keep strategy1."""

import sqlalchemy as sa
from alembic import op

revision = "0009_user_strategy"
down_revision = "0008_important_announcements"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("strategy_id", sa.String(32), nullable=False, server_default="strategy1"))


def downgrade():
    op.drop_column("users", "strategy_id")
