"""Preserve original budget currencies and timestamped automatic AUD/USD quotes."""
from alembic import op
import sqlalchemy as sa

revision = "0017_aud_cash"
down_revision = "0016_daily_report_version"
branch_labels = depends_on = None


def upgrade():
    op.add_column('strategy_cash', sa.Column('currency', sa.String(3), nullable=False, server_default='USD'))
    op.add_column('user_signals', sa.Column('fx_note', sa.Text(), nullable=True))
    op.create_table('fx_quotes',
        sa.Column('pair', sa.String(12), primary_key=True),
        sa.Column('aud_usd', sa.Numeric(20, 10), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(40), nullable=False))


def downgrade():
    # Mixed-currency budgets cannot be silently relabelled USD on downgrade.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM strategy_cash WHERE currency != 'USD'")).scalar():
        raise RuntimeError('请先将独立现金预算改为USD，再降级澳元适配迁移。')
    op.drop_table('fx_quotes')
    with op.batch_alter_table('user_signals') as batch:
        batch.drop_column('fx_note')
    with op.batch_alter_table('strategy_cash') as batch:
        batch.drop_column('currency')
