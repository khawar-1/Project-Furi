"""goal_threads (Phase 11 Part 3 — ongoing-concern tracking)

Revision ID: f4b7d9a1c025
Revises: e2c4a6b8d013
Create Date: 2026-07-16

Idempotent: create_all may build the table first on a fresh DB (the model
carries it), so guard with has_table — the suggestions-migration pattern.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4b7d9a1c025'
down_revision: Union[str, None] = 'e2c4a6b8d013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table('goal_threads'):
        return  # create_all raced ahead — nothing to do
    op.create_table(
        'goal_threads',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('normalized_title', sa.String(length=512), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('contact_id', sa.String(length=36), nullable=True),
        sa.Column('event_date', sa.Date(), nullable=True),
        sa.Column('next_check_at', sa.DateTime(), nullable=True),
        sa.Column('last_nudged_at', sa.DateTime(), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False, server_default='extractor'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['contact_id'], ['contacts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('goal_threads', schema=None) as batch_op:
        batch_op.create_index('ix_goal_threads_normalized_title', ['normalized_title'])
        batch_op.create_index('ix_goal_threads_status', ['status'])
        batch_op.create_index('ix_goal_threads_contact_id', ['contact_id'])
        batch_op.create_index('ix_goal_threads_next_check_at', ['next_check_at'])
        batch_op.create_index('ix_goal_threads_is_active', ['is_active'])


def downgrade() -> None:
    with op.batch_alter_table('goal_threads', schema=None) as batch_op:
        batch_op.drop_index('ix_goal_threads_is_active')
        batch_op.drop_index('ix_goal_threads_next_check_at')
        batch_op.drop_index('ix_goal_threads_contact_id')
        batch_op.drop_index('ix_goal_threads_status')
        batch_op.drop_index('ix_goal_threads_normalized_title')
    op.drop_table('goal_threads')
