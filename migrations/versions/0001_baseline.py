"""baseline - reflects current schema already live in production

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inquiries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String),
        sa.Column("phone", sa.String),
        sa.Column("preferred_time", sa.String),
        sa.Column("appointment_date", sa.Date),
        sa.Column("appointment_time", sa.String),
        sa.Column("appointment_type", sa.String, server_default="physical"),
        sa.Column("payment_screenshot_path", sa.String),
        sa.Column("confirmation_code", sa.String),
        sa.Column("created_at", sa.DateTime),
        sa.Column("status", sa.String, server_default="New"),
    )
    op.create_table(
        "contact_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String),
        sa.Column("contact_info", sa.String),
        sa.Column("message", sa.String),
        sa.Column("created_at", sa.DateTime),
        sa.Column("read", sa.String, server_default="Unread"),
    )


def downgrade() -> None:
    op.drop_table("contact_messages")
    op.drop_table("inquiries")
