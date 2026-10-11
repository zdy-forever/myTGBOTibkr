"""Allow a user to enable multiple strategies without losing prior selections."""

import sqlalchemy as sa

from alembic import op

revision = "0011_multi_strategy"
down_revision = "0010_strategy_selection_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_strategies",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("strategy_id", sa.String(32), primary_key=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO user_strategies (user_id, strategy_id, enabled_at) "
               "SELECT id, strategy_id, created_at FROM users WHERE strategy_id IS NOT NULL")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("strategy_id")


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("strategy_id", sa.String(32), nullable=True))
    op.execute("UPDATE users SET strategy_id = "
               "(SELECT strategy_id FROM user_strategies WHERE user_strategies.user_id = users.id "
               "ORDER BY enabled_at LIMIT 1)")
    op.drop_table("user_strategies")
