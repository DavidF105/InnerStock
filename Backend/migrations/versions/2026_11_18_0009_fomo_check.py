"""trades: stored FOMO check (score, band, signals) and the patience test

All nullable - existing trades are scored lazily by fomo.ensure_scored the
first time they're needed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(sa.Column("fomo_score", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("fomo_band", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("fomo_signals", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("fomo_checked_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("week_later_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_column("week_later_pct")
        batch_op.drop_column("fomo_checked_at")
        batch_op.drop_column("fomo_signals")
        batch_op.drop_column("fomo_band")
        batch_op.drop_column("fomo_score")
