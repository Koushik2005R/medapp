"""add persisted alarm firings

Revision ID: 4f9a2c6d81be
Revises: 9e09a6d778d7
Create Date: 2026-09-27 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4f9a2c6d81be"
down_revision = "9e09a6d778d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alarm_firings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("firing_date", sa.Date(), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reminder_id", "firing_date", name="uq_alarm_firing_reminder_date"
        ),
    )
    op.create_index(
        "ix_alarm_firings_reminder_id", "alarm_firings", ["reminder_id"]
    )


def downgrade():
    op.drop_index("ix_alarm_firings_reminder_id", table_name="alarm_firings")
    op.drop_table("alarm_firings")
