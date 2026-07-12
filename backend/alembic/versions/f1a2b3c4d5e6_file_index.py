"""file_index table (Phase 6 Part 2 — semantic file index ledger)

Revision ID: f1a2b3c4d5e6
Revises: e7b93c250a41
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e7b93c250a41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'file_index',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('path', sa.String(length=1024), nullable=False),
        sa.Column('folder_root', sa.String(length=1024), nullable=False),
        sa.Column('filename', sa.String(length=512), nullable=True),
        sa.Column('ext', sa.String(length=32), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('mtime', sa.Float(), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('indexed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('file_index', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_file_index_path'), ['path'], unique=True)
        batch_op.create_index(batch_op.f('ix_file_index_is_active'), ['is_active'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('file_index', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_file_index_is_active'))
        batch_op.drop_index(batch_op.f('ix_file_index_path'))
    op.drop_table('file_index')
