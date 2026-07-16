"""routine schedule fields (Phase 10 Part 2 — scheduled routines)

Revision ID: e2c4a6b8d013
Revises: f9c1a7b3d2e8
Create Date: 2026-07-16

Adds an optional time trigger to routines. Idempotent: create_all builds a
fresh DB's `routines` table WITH these columns already (the model carries
them), and this migration must no-op on any column that already exists — the
add-column analogue of the create_all-race guard the table migrations use.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2c4a6b8d013'
down_revision: Union[str, None] = 'f9c1a7b3d2e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLUMNS = (
    ('schedule_type', sa.Column('schedule_type', sa.String(length=16), nullable=True)),
    ('schedule_minute', sa.Column('schedule_minute', sa.Integer(), nullable=False, server_default='0')),
    ('schedule_hour', sa.Column('schedule_hour', sa.Integer(), nullable=False, server_default='9')),
    ('schedule_weekday', sa.Column('schedule_weekday', sa.Integer(), nullable=True)),
    ('schedule_interval_minutes', sa.Column('schedule_interval_minutes', sa.Integer(), nullable=True)),
    ('schedule_job_id', sa.Column('schedule_job_id', sa.String(length=36), nullable=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('routines'):
        return  # fresh DB where create_all hasn't run yet — model builds it
    existing = {c['name'] for c in inspector.get_columns('routines')}
    with op.batch_alter_table('routines', schema=None) as batch_op:
        for name, column in _NEW_COLUMNS:
            if name not in existing:
                batch_op.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table('routines', schema=None) as batch_op:
        for name, _ in reversed(_NEW_COLUMNS):
            batch_op.drop_column(name)
