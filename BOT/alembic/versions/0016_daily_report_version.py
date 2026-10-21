"""Invalidate daily strategy reports when the strategy implementation changes."""
from alembic import op
import sqlalchemy as sa

revision = "0016_daily_report_version"
down_revision = "0015_daily_strategies"
branch_labels = depends_on = None


def upgrade():
    op.add_column('daily_reports', sa.Column('strategy_version', sa.String(80), nullable=True))


def downgrade():
    with op.batch_alter_table('daily_reports') as batch:
        batch.drop_column('strategy_version')
