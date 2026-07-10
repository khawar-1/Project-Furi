"""scheduled jobs (Phase 4 Part 2 — scheduler/event bus)

Revision ID: d3f7a2c81e94
Revises: b7d21c904f3a
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f7a2c81e94'
down_revision: Union[str, None] = 'b7d21c904f3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('scheduled_jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('run_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('fired_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('scheduled_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scheduled_jobs_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_scheduled_jobs_run_at'), ['run_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_scheduled_jobs_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('scheduled_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_scheduled_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_scheduled_jobs_run_at'))
        batch_op.drop_index(batch_op.f('ix_scheduled_jobs_kind'))

    op.drop_table('scheduled_jobs')
