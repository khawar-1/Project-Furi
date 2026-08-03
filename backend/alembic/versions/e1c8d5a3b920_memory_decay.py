"""semantic_memories: last_used_at + archived_at (memory decay + reversible archive)

Revision ID: e1c8d5a3b920
Revises: d9b3e4f70a15
Create Date: 2026-08-03

Two nullable columns, no data change. `last_used_at` records when a fact was
last RENDERED into a MEMORY CONTEXT block; `archived_at` is set by the
housekeeping archive pass and hides a fact from retrieval WITHOUT deleting it.

Existing rows get NULL for both, which reads as "never used, never archived" —
the correct starting state: nothing is archived until a fact has been observably
unused for months, and the clock starts now rather than retroactively.

Idempotent: create_all may have built the columns first on a fresh DB (the model
carries them), so each add is guarded — the routing_decisions/goal_threads
pattern applied to ALTER instead of CREATE.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1c8d5a3b920'
down_revision: Union[str, None] = 'd9b3e4f70a15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'semantic_memories'


def _columns(bind) -> set[str]:
    return {c['name'] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if 'last_used_at' not in existing:
            batch_op.add_column(sa.Column('last_used_at', sa.DateTime(), nullable=True))
        if 'archived_at' not in existing:
            batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))

    indexes = {i['name'] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if 'ix_semantic_memories_archived_at' not in indexes:
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.create_index('ix_semantic_memories_archived_at', ['archived_at'])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_index('ix_semantic_memories_archived_at')
        batch_op.drop_column('archived_at')
        batch_op.drop_column('last_used_at')
