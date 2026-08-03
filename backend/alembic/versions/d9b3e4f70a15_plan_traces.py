"""plan_traces — the audit trail for WHY a plan gave up

Revision ID: d9b3e4f70a15
Revises: c7f2a5e91d84
Create Date: 2026-08-03

The routing_decisions sibling, one layer down: routing records what Jarvis
didn't DO, this records why a plan it did take gave up. See
app/core/plan_trace.py for what was unrecorded before it.

Idempotent: create_all may build the table first on a fresh DB (the model
carries it), so guard with has_table — the routing_decisions/goal_threads
pattern.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd9b3e4f70a15'
down_revision: Union[str, None] = 'c7f2a5e91d84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table('plan_traces'):
        return  # create_all raced ahead — nothing to do
    op.create_table(
        'plan_traces',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('plan_id', sa.String(length=36), nullable=True),
        sa.Column('task_id', sa.String(length=36), nullable=True),
        sa.Column('goal', sa.Text(), nullable=True),
        sa.Column('goal_chars', sa.Integer(), nullable=True),
        sa.Column('agent_key', sa.String(length=32), nullable=True),
        sa.Column('entry', sa.String(length=16), nullable=True),
        sa.Column('execution', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=24), nullable=True),
        sa.Column('fail_class', sa.String(length=32), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('steps_total', sa.Integer(), nullable=True),
        sa.Column('steps_completed', sa.Integer(), nullable=True),
        sa.Column('steps_failed', sa.Integer(), nullable=True),
        sa.Column('steps_skipped', sa.Integer(), nullable=True),
        sa.Column('replan_count', sa.Integer(), nullable=True),
        sa.Column('questions_asked', sa.Integer(), nullable=True),
        sa.Column('rejections', sa.Text(), nullable=True),
        sa.Column('rejection_count', sa.Integer(), nullable=True),
        sa.Column('failed_tool', sa.String(length=128), nullable=True),
        sa.Column('failed_signature', sa.Text(), nullable=True),
        sa.Column('failed_error', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('plan_traces', schema=None) as batch_op:
        batch_op.create_index('ix_plan_traces_session_id', ['session_id'])
        batch_op.create_index('ix_plan_traces_created_at', ['created_at'])
        batch_op.create_index('ix_plan_traces_plan_id', ['plan_id'])
        batch_op.create_index('ix_plan_traces_status', ['status'])
        batch_op.create_index('ix_plan_traces_fail_class', ['fail_class'])
        batch_op.create_index('ix_plan_traces_failed_tool', ['failed_tool'])


def downgrade() -> None:
    with op.batch_alter_table('plan_traces', schema=None) as batch_op:
        batch_op.drop_index('ix_plan_traces_failed_tool')
        batch_op.drop_index('ix_plan_traces_fail_class')
        batch_op.drop_index('ix_plan_traces_status')
        batch_op.drop_index('ix_plan_traces_plan_id')
        batch_op.drop_index('ix_plan_traces_created_at')
        batch_op.drop_index('ix_plan_traces_session_id')
    op.drop_table('plan_traces')
