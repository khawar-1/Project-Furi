"""autofill_fields (Phase 15.2 — the grounded autofill profile)

Revision ID: a3f5c9e17b42
Revises: f4b7d9a1c025
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f5c9e17b42'
down_revision: Union[str, None] = 'f4b7d9a1c025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: startup auto-runs migrations (2026-07-13) AFTER init_db()'s
    # create_all, so a dev database may already have the table — tolerate
    # create_all having raced ahead (the suggestions/goal_threads guard, for a
    # whole new table).
    inspector = sa.inspect(op.get_bind())
    if "autofill_fields" in inspector.get_table_names():
        return

    op.create_table(
        'autofill_fields',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('autofill_fields', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_autofill_fields_key'), ['key'], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table('autofill_fields', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_autofill_fields_key'))

    op.drop_table('autofill_fields')
