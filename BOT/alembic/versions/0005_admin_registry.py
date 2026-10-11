"""Persist administrator grants while keeping Bot Owner outside the database."""
from alembic import op
import sqlalchemy as sa

revision = "0005_admin_registry"
down_revision = "0004_premium_access"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_grants",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column("label", sa.String(80)),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "admin_bootstrap",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("admin_bootstrap")
    op.drop_table("admin_grants")
