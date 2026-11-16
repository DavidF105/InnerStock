"""email confirmation, password reset, recap emails and demo accounts

  - users.email_verified / users.is_demo
  - earnings_notifications.emailed_at ("Email me this")
  - email_tokens: hashed one-time links for email confirmation and
    password reset

Existing users start unconfirmed (false) and are not demo accounts.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("email_verified", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column("is_demo", sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table("earnings_notifications") as batch_op:
        batch_op.add_column(sa.Column("emailed_at", sa.DateTime(), nullable=True))
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )


def downgrade() -> None:
    op.drop_table("email_tokens")
    with op.batch_alter_table("earnings_notifications") as batch_op:
        batch_op.drop_column("emailed_at")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("is_demo")
        batch_op.drop_column("email_verified")
