"""routines (Phase 6 Part 5 — teachable procedural memory)

Revision ID: a1b2c3d4e5f6
Revises: d4e5f6a7b8c9
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: init_db()'s create_all may already have built this table
    # (the create_all-vs-alembic drift the startup auto-migration tolerates).
    if sa.inspect(op.get_bind()).has_table('routines'):
        return
    op.create_table('routines',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('normalized_name', sa.String(length=256), nullable=False),
    sa.Column('goal_template', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('routines', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_routines_normalized_name'), ['normalized_name'], unique=True
        )
        batch_op.create_index(batch_op.f('ix_routines_is_active'), ['is_active'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('routines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_routines_is_active'))
        batch_op.drop_index(batch_op.f('ix_routines_normalized_name'))

    op.drop_table('routines')
