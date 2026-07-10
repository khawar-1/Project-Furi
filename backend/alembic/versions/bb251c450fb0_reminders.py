"""reminders (Phase 4 Part 4 — reminders end-to-end)

Revision ID: bb251c450fb0
Revises: d3f7a2c81e94
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb251c450fb0'
down_revision: Union[str, None] = 'd3f7a2c81e94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('reminders',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=True),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('fired_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reminders_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_reminders_due_at'), ['due_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_reminders_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reminders_status'))
        batch_op.drop_index(batch_op.f('ix_reminders_due_at'))
        batch_op.drop_index(batch_op.f('ix_reminders_session_id'))

    op.drop_table('reminders')
