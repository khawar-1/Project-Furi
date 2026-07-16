"""suggestions (Phase 9 — the Initiative Engine)

Revision ID: f9c1a7b3d2e8
Revises: c3d4e5f6a7b8
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9c1a7b3d2e8'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: a dev database built by init_db()'s create_all already has the
    # table — startup auto-runs migrations (2026-07-13), so this must tolerate
    # create_all having raced ahead (the message_embedded_at guard, for a whole
    # new table this time).
    inspector = sa.inspect(op.get_bind())
    if "suggestions" in inspector.get_table_names():
        return

    op.create_table(
        'suggestions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=True),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False),
        sa.Column('autonomy', sa.String(length=16), nullable=False),
        sa.Column('priority', sa.String(length=16), nullable=False),
        sa.Column('goal', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=True),
        sa.Column('dedupe_key', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('suggestions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_suggestions_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_suggestions_category'), ['category'], unique=False)
        batch_op.create_index(batch_op.f('ix_suggestions_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_suggestions_dedupe_key'), ['dedupe_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_suggestions_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_suggestions_expires_at'), ['expires_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('suggestions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_suggestions_expires_at'))
        batch_op.drop_index(batch_op.f('ix_suggestions_created_at'))
        batch_op.drop_index(batch_op.f('ix_suggestions_dedupe_key'))
        batch_op.drop_index(batch_op.f('ix_suggestions_status'))
        batch_op.drop_index(batch_op.f('ix_suggestions_category'))
        batch_op.drop_index(batch_op.f('ix_suggestions_session_id'))

    op.drop_table('suggestions')
