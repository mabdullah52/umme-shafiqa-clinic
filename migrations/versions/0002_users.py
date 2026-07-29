"""add users table for JWT auth with roles

Revision ID: 0002_users
Revises: 0001_baseline
Create Date: 2026-07-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0002_users"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String, unique=True, nullable=False),
        sa.Column("hashed_password", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False, server_default="patient"),  # 'admin', 'receptionist', or 'patient'
        sa.Column("name", sa.String),
        sa.Column("phone", sa.String),
        sa.Column("created_at", sa.DateTime),
    )


def downgrade() -> None:
    op.drop_table("users")
