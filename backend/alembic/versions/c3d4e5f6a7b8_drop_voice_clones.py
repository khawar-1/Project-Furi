"""drop voice_clones (TTS switched Chatterbox → Kokoro; no voice cloning)

Kokoro has fixed preset voices only, so the voice-cloning feature and its
`voice_clones` table are removed. The b2c3d4e5f6a7 migration that created the
table is deliberately kept for history integrity (already-migrated DBs); this
revision drops the table on top.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: tolerate a DB where the table was never built (a fresh
    # create_all no longer knows the ORM model), matching the startup
    # auto-migration's create_all-vs-alembic drift stance.
    if not sa.inspect(op.get_bind()).has_table('voice_clones'):
        return
    with op.batch_alter_table('voice_clones', schema=None) as batch_op:
        try:
            batch_op.drop_index(batch_op.f('ix_voice_clones_status'))
        except Exception:
            # Index may not exist on some create_all-built copies — non-fatal.
            pass
    op.drop_table('voice_clones')


def downgrade() -> None:
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
