"""Require a strategy choice for new users without changing existing selections."""

import sqlalchemy as sa

from alembic import op

revision = "0010_strategy_selection_gate"
down_revision = "0009_user_strategy"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.alter_column("strategy_id", existing_type=sa.String(32), nullable=True, server_default=None)


def downgrade():
    op.execute("UPDATE users SET strategy_id = 'strategy1' WHERE strategy_id IS NULL")
    with op.batch_alter_table("users") as batch:
        batch.alter_column("strategy_id", existing_type=sa.String(32), nullable=False, server_default="strategy1")
