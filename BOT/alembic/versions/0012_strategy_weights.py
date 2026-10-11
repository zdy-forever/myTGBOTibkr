"""Persist explicit strategy allocations; multiple existing strategies need input."""
import sqlalchemy as sa

from alembic import op

revision = "0012_strategy_weights"
down_revision = "0011_multi_strategy"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_strategies") as batch:
        batch.add_column(sa.Column("weight_percent", sa.Numeric(5, 2), nullable=True))
        batch.create_check_constraint("ck_strategy_weight", "weight_percent > 0 AND weight_percent <= 100")
    op.execute("UPDATE user_strategies SET weight_percent = 100 WHERE user_id IN "
               "(SELECT user_id FROM user_strategies GROUP BY user_id HAVING COUNT(*) = 1)")


def downgrade():
    with op.batch_alter_table("user_strategies") as batch:
        batch.drop_constraint("ck_strategy_weight", type_="check")
        batch.drop_column("weight_percent")
