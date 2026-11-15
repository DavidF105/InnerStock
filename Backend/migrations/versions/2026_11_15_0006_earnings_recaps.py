"""earnings recaps + notifications

  - earnings_recaps: Claude's write-up of one company's earnings report,
    shared by every user who held the stock (one per ticker + report date)
  - earnings_notifications: one row per user per recap, read_at NULL
    until the user has seen it

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "earnings_recaps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("eps_estimate", sa.Float(), nullable=True),
        sa.Column("eps_actual", sa.Float(), nullable=True),
        sa.Column("surprise_pct", sa.Float(), nullable=True),
        sa.Column("reaction_pct", sa.Float(), nullable=True),
        sa.Column("move_since_pct", sa.Float(), nullable=True),
        sa.Column("headline", sa.String(), nullable=False),
        sa.Column("what_happened", sa.Text(), nullable=False),
        sa.Column("what_it_means", sa.Text(), nullable=False),
        sa.Column("sentiment", sa.String(), nullable=False),
        sa.Column("sources", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", "report_date", name="uq_earnings_recaps_ticker_date"),
    )
    op.create_table(
        "earnings_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("recap_id", sa.Integer(), nullable=False),
        sa.Column("display_ticker", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["recap_id"], ["earnings_recaps.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "recap_id", name="uq_earnings_notifications_user_recap"),
    )


def downgrade() -> None:
    op.drop_table("earnings_notifications")
    op.drop_table("earnings_recaps")
