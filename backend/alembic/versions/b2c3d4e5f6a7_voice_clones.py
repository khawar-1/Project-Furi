"""voice_clones (Phase 7 — voice cloning)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: init_db()'s create_all may already have built this table
    # (the create_all-vs-alembic drift the startup auto-migration tolerates).
    if sa.inspect(op.get_bind()).has_table('voice_clones'):
        return
    op.create_table('voice_clones',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('reference_path', sa.String(length=1024), nullable=False),
    sa.Column('conds_path', sa.String(length=1024), nullable=False),
    sa.Column('sample_rate', sa.Integer(), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('voice_clones', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_voice_clones_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('voice_clones', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_voice_clones_status'))
    op.drop_table('voice_clones')
