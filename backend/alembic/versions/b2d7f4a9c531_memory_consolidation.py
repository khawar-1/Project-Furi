"""contacts.history_digest + memory_conflicts (consolidation + conflict capture)

Revision ID: b2d7f4a9c531
Revises: e1c8d5a3b920
Create Date: 2026-08-04

The second half of Tier 2 item 7. `e1c8d5a3b920` bounded memory (decay ranking +
reversible archive); this makes what falls outside the budget still USEFUL, and
stops throwing away a conflict the extractor already found.

Two nullable columns on `contacts`:
  history_digest       composed prose covering the contact's OLDER fact-log
                       entries, so the prompt carries their substance instead of
                       a count. Existing rows get NULL, which reads as "never
                       consolidated" — exactly today's behaviour.
  history_digest_upto  the interaction_date of the newest fact that digest
                       covers: both the recompose watermark and what the UI
                       shows.

And one new table, `memory_conflicts` — see the model docstring. Nothing
backfills it: it fills from the extraction path as conflicts occur.

Idempotent, because create_all may have built either first on a fresh DB (the
models carry both) — the goal_threads/routing_decisions pattern, applied to an
ALTER and a CREATE in the same revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d7f4a9c531'
down_revision: Union[str, None] = 'e1c8d5a3b920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONTACTS = 'contacts'
_CONFLICTS = 'memory_conflicts'


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = {c['name'] for c in inspector.get_columns(_CONTACTS)}
    with op.batch_alter_table(_CONTACTS, schema=None) as batch_op:
        if 'history_digest' not in existing:
            batch_op.add_column(sa.Column('history_digest', sa.Text(), nullable=True))
        if 'history_digest_upto' not in existing:
            batch_op.add_column(
                sa.Column('history_digest_upto', sa.DateTime(), nullable=True)
            )

    if _CONFLICTS not in inspector.get_table_names():
        op.create_table(
            _CONFLICTS,
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('memory_id', sa.String(length=36), nullable=False),
            sa.Column('old_content', sa.Text(), nullable=False),
            sa.Column('new_content', sa.Text(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('detected_at', sa.DateTime(), nullable=False),
            sa.Column('resolved_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            f'ix_{_CONFLICTS}_memory_id', _CONFLICTS, ['memory_id'], unique=False
        )
        op.create_index(
            f'ix_{_CONFLICTS}_status', _CONFLICTS, ['status'], unique=False
        )


def downgrade() -> None:
    op.drop_index(f'ix_{_CONFLICTS}_status', table_name=_CONFLICTS)
    op.drop_index(f'ix_{_CONFLICTS}_memory_id', table_name=_CONFLICTS)
    op.drop_table(_CONFLICTS)
    with op.batch_alter_table(_CONTACTS, schema=None) as batch_op:
        batch_op.drop_column('history_digest_upto')
        batch_op.drop_column('history_digest')
