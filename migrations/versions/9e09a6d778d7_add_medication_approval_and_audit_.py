"""add medication approval and audit workflow

Revision ID: 9e09a6d778d7
Revises: a908307e5a50
Create Date: 2026-09-26 20:39:07.360938

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9e09a6d778d7'
down_revision = 'a908307e5a50'
branch_labels = None
depends_on = None


def upgrade():
    from app import db

    bind = op.get_bind()
    for table_name in ("medication_change_requests", "medication_audit"):
        db.metadata.tables[table_name].create(bind=bind, checkfirst=True)
    medication_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("medications")
    }
    with op.batch_alter_table("medications") as batch:
        if "status" not in medication_columns:
            batch.add_column(sa.Column("status", sa.String(20), nullable=False, server_default="Active"))
        if "created_at" not in medication_columns:
            batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        if "updated_at" not in medication_columns:
            batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    notification_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("notifications")
    }
    if "recipient_id" not in notification_columns:
        with op.batch_alter_table("notifications") as batch:
            batch.add_column(sa.Column(
                "recipient_id",
                sa.Integer(),
                sa.ForeignKey("users.id", name="fk_notifications_recipient_id_users"),
                nullable=True,
            ))
    notification_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("notifications")
    }
    if "ix_notifications_recipient_id" not in notification_indexes:
        op.create_index("ix_notifications_recipient_id", "notifications", ["recipient_id"])
    bind.execute(sa.text("UPDATE medications SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
    bind.execute(sa.text("UPDATE medications SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
    if bind.dialect.name == "sqlite":
        bind.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS medication_audit_no_update
            BEFORE UPDATE ON medication_audit
            BEGIN SELECT RAISE(ABORT, 'medication audit records are immutable'); END
        """))
        bind.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS medication_audit_no_delete
            BEFORE DELETE ON medication_audit
            BEGIN SELECT RAISE(ABORT, 'medication audit records cannot be deleted'); END
        """))


def downgrade():
    raise RuntimeError("Medication requests and audit history must not be destructively downgraded.")
