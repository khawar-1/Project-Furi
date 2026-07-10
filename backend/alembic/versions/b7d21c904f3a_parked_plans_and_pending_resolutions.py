"""parked plans and pending resolutions (Phase 3.5 persistence)

Revision ID: b7d21c904f3a
Revises: eca5df131069
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d21c904f3a'
down_revision: Union[str, None] = 'eca5df131069'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('parked_plans',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('parked_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_parked_plans_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_parked_plans_expires_at'), ['expires_at'], unique=False)

    op.create_table('pending_resolutions',
    sa.Column('session_id', sa.String(length=36), nullable=False),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.Column('creation', sa.Text(), nullable=True),
    sa.Column('confirmed_names', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('session_id')
    )
    with op.batch_alter_table('pending_resolutions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pending_resolutions_expires_at'), ['expires_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('pending_resolutions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pending_resolutions_expires_at'))

    op.drop_table('pending_resolutions')
    with op.batch_alter_table('parked_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_parked_plans_expires_at'))
        batch_op.drop_index(batch_op.f('ix_parked_plans_session_id'))

    op.drop_table('parked_plans')
