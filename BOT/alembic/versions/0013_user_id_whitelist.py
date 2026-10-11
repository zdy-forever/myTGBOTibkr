"""Allow ID whitelist grants without bypassing phone registration."""
import sqlalchemy as sa

from alembic import op

revision = "0013_user_id_whitelist"
down_revision = "0012_strategy_weights"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_id_whitelist",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("label", sa.String(80)),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("user_id_whitelist")
