"""messages.embedded_at (Phase 6 Part 4 — conversation search cursor)

Revision ID: d4e5f6a7b8c9
Revises: f1a2b3c4d5e6
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: a dev database built by init_db()'s create_all already has
    # the column — upgrading over that drift must not explode (2026-07-13:
    # startup now auto-runs migrations, so they must tolerate create_all
    # having raced ahead).
    inspector = sa.inspect(op.get_bind())
    if any(c["name"] == "embedded_at" for c in inspector.get_columns("messages")):
        return
    # NULL for every existing row → the backfill pass embeds history once.
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('embedded_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_column('embedded_at')
