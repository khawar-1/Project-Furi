"""task domain (boss + domain-specialized agents)

Revision ID: b8e1d3f0a2c5
Revises: a3f5c9e17b42
Create Date: 2026-07-24

Adds Task.domain — which domain agent (file / email / calendar / research /
browser / general) owns a background task. Used by the Agents panel, progress
queries, and telemetry. Idempotent: create_all builds a fresh DB's `tasks`
table WITH this column already (the model carries it), and this migration
no-ops when the column exists — the add-column analogue of the create_all-race
guard the table migrations use (mirrors e2c4a6b8d013).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e1d3f0a2c5'
down_revision: Union[str, None] = 'a3f5c9e17b42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('tasks'):
        return  # fresh DB where create_all hasn't run yet — model builds it
    existing = {c['name'] for c in inspector.get_columns('tasks')}
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        if 'domain' not in existing:
            batch_op.add_column(sa.Column('domain', sa.String(length=24), nullable=True))
    index_names = {ix['name'] for ix in inspector.get_indexes('tasks')}
    if 'ix_tasks_domain' not in index_names:
        op.create_index('ix_tasks_domain', 'tasks', ['domain'])


def downgrade() -> None:
    op.drop_index('ix_tasks_domain', table_name='tasks')
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('domain')
