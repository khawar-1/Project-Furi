"""app_settings key/value table (Phase 5 Part 6 — daily briefing config)

Revision ID: e7b93c250a41
Revises: c1a4e8f60b23
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b93c250a41'
down_revision: Union[str, None] = 'c1a4e8f60b23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
