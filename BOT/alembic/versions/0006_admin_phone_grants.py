"""Allow phone-based admin grants without retaining phone numbers."""
from alembic import op
import sqlalchemy as sa

revision = "0006_admin_phone_grants"
down_revision = "0005_admin_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_phone_grants",
        sa.Column("phone_fingerprint", sa.String(64), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger()),
        sa.Column("label", sa.String(80)),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_phone_grants_telegram_id", "admin_phone_grants", ["telegram_id"])


def downgrade():
    op.drop_table("admin_phone_grants")
