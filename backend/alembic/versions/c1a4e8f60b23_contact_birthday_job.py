"""contact birthday_job_id (Phase 5 Part 4 — recurring birthday reminders)

Revision ID: c1a4e8f60b23
Revises: a9e4c07d5b12
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a4e8f60b23'
down_revision: Union[str, None] = 'a9e4c07d5b12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('contacts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('birthday_job_id', sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('contacts', schema=None) as batch_op:
        batch_op.drop_column('birthday_job_id')
