"""routing_decisions — the audit trail for what Furi DIDN'T do

Revision ID: c7f2a5e91d84
Revises: b8e1d3f0a2c5
Create Date: 2026-08-03

Idempotent: create_all may build the table first on a fresh DB (the model
carries it), so guard with has_table — the goal_threads/suggestions pattern.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7f2a5e91d84'
down_revision: Union[str, None] = 'b8e1d3f0a2c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table('routing_decisions'):
        return  # create_all raced ahead — nothing to do
    op.create_table(
        'routing_decisions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('message_chars', sa.Integer(), nullable=True),
        sa.Column('has_conversation', sa.Boolean(), nullable=True),
        sa.Column('gate_fired', sa.Boolean(), nullable=True),
        sa.Column('gate_reason', sa.String(length=32), nullable=True),
        sa.Column('label', sa.String(length=16), nullable=True),
        sa.Column('mode', sa.String(length=16), nullable=True),
        sa.Column('classifier_ms', sa.Integer(), nullable=True),
        sa.Column('classifier_error', sa.String(length=256), nullable=True),
        sa.Column('classifier_model', sa.String(length=64), nullable=True),
        sa.Column('bare_navigation', sa.Boolean(), nullable=True),
        sa.Column('background_intent', sa.Boolean(), nullable=True),
        sa.Column('agent', sa.String(length=32), nullable=True),
        sa.Column('execution', sa.String(length=16), nullable=True),
        sa.Column('outcome', sa.String(length=24), nullable=True),
        sa.Column('fail_open_reason', sa.String(length=24), nullable=True),
        sa.Column('rescue_fired', sa.Boolean(), nullable=True),
        sa.Column('rescue_ok', sa.Boolean(), nullable=True),
        sa.Column('impersonation_cut', sa.Boolean(), nullable=True),
        sa.Column('route_ms', sa.Integer(), nullable=True),
        sa.Column('task_id', sa.String(length=36), nullable=True),
        sa.Column('plan_id', sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('routing_decisions', schema=None) as batch_op:
        batch_op.create_index('ix_routing_decisions_session_id', ['session_id'])
        batch_op.create_index('ix_routing_decisions_created_at', ['created_at'])
        batch_op.create_index('ix_routing_decisions_outcome', ['outcome'])
        batch_op.create_index('ix_routing_decisions_fail_open_reason', ['fail_open_reason'])
        batch_op.create_index('ix_routing_decisions_label', ['label'])


def downgrade() -> None:
    with op.batch_alter_table('routing_decisions', schema=None) as batch_op:
        batch_op.drop_index('ix_routing_decisions_label')
        batch_op.drop_index('ix_routing_decisions_fail_open_reason')
        batch_op.drop_index('ix_routing_decisions_outcome')
        batch_op.drop_index('ix_routing_decisions_created_at')
        batch_op.drop_index('ix_routing_decisions_session_id')
    op.drop_table('routing_decisions')
