"""add medication timezone and sensor events

Revision ID: a908307e5a50
Revises:
Create Date: 2026-09-26 20:28:03.092793

"""
import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a908307e5a50'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    from app import db

    bind = op.get_bind()
    initial_tables = [
        table for name, table in db.metadata.tables.items()
        if name not in {"medication_change_requests", "medication_audit"}
    ]
    db.metadata.create_all(bind=bind, tables=initial_tables, checkfirst=True)
    inspector = sa.inspect(bind)
    additions = {
        "users": {"timezone": sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC")},
        "reminders": {
            "medication_id": sa.Column("medication_id", sa.Integer(), nullable=True),
            "compartment": sa.Column("compartment", sa.Integer(), nullable=True),
            "tablet_weight": sa.Column("tablet_weight", sa.Float(), nullable=False, server_default="0.5"),
            "expected_quantity": sa.Column("expected_quantity", sa.Integer(), nullable=False, server_default="1"),
            "tolerance": sa.Column("tolerance", sa.Float(), nullable=False, server_default="0.2"),
            "calibration_offset": sa.Column("calibration_offset", sa.Float(), nullable=False, server_default="0.0"),
            "response_window_minutes": sa.Column("response_window_minutes", sa.Integer(), nullable=False, server_default="30"),
            "noise_threshold": sa.Column("noise_threshold", sa.Float(), nullable=False, server_default="0.15"),
        },
        "pill_logs": {
            "reminder_id": sa.Column("reminder_id", sa.Integer(), nullable=True),
            "dose_event_id": sa.Column("dose_event_id", sa.Integer(), nullable=True),
            "compartment": sa.Column("compartment", sa.Integer(), nullable=True),
            "source": sa.Column("source", sa.String(30), nullable=True),
            "verification_method": sa.Column("verification_method", sa.String(50), nullable=True),
        },
        "hardware_traffic": {
            "patient_id": sa.Column("patient_id", sa.Integer(), nullable=True),
        },
    }
    foreign_keys = {
        "reminders": (("medication_id", "medications", "fk_reminders_medication_id"),),
        "pill_logs": (
            ("reminder_id", "reminders", "fk_pill_logs_reminder_id"),
            ("dose_event_id", "dose_events", "fk_pill_logs_dose_event_id"),
        ),
        "hardware_traffic": (("patient_id", "users", "fk_hardware_traffic_patient_id"),),
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        known_foreign_keys = {
            (tuple(key["constrained_columns"]), key["referred_table"])
            for key in inspector.get_foreign_keys(table)
        }
        missing_foreign_keys = [
            (column, referred_table, name)
            for column, referred_table, name in foreign_keys.get(table, ())
            if ((column,), referred_table) not in known_foreign_keys
        ]
        missing_columns = {
            name: column for name, column in columns.items() if name not in existing
        }
        if missing_foreign_keys:
            with op.batch_alter_table(table) as batch:
                for name, column in missing_columns.items():
                    batch.add_column(column)
                for column, referred_table, name in missing_foreign_keys:
                    batch.create_foreign_key(
                        name, referred_table, [column], ["id"]
                    )
        else:
            for column in missing_columns.values():
                op.add_column(table, column)
    if "reminders" in inspector.get_table_names() and (
        "ix_reminders_medication_id"
        not in {index["name"] for index in sa.inspect(bind).get_indexes("reminders")}
    ):
        op.create_index("ix_reminders_medication_id", "reminders", ["medication_id"])
    if "hardware_traffic" in inspector.get_table_names() and (
        "ix_hardware_traffic_patient_id"
        not in {index["name"] for index in sa.inspect(bind).get_indexes("hardware_traffic")}
    ):
        op.create_index(
            "ix_hardware_traffic_patient_id",
            "hardware_traffic",
            ["patient_id"],
        )

    bind.execute(sa.text("""
        INSERT INTO medications (patient_id, name, status, created_at, updated_at)
        SELECT DISTINCT reminders.patient_id, reminders.med_name, 'Active',
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM reminders
        WHERE NOT EXISTS (
            SELECT 1 FROM medications
            WHERE medications.patient_id = reminders.patient_id
              AND medications.name = reminders.med_name
        )
    """))
    bind.execute(sa.text("""
        UPDATE reminders
        SET medication_id = (
            SELECT medications.id
            FROM medications
            WHERE medications.patient_id = reminders.patient_id
              AND medications.name = reminders.med_name
        )
        WHERE medication_id IS NULL
    """))
    if "hardware_traffic" in inspector.get_table_names():
        allowed_payload_fields = {
            "patient_id", "reminder_id", "compartment", "w_before",
            "w_after", "samples_before", "samples_after",
        }
        for row in bind.execute(sa.text(
            "SELECT id, payload FROM hardware_traffic"
        )).mappings():
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError(f"hardware traffic row {row['id']} has an invalid payload")
            safe_payload = {
                key: value for key, value in payload.items()
                if key in allowed_payload_fields
            }
            patient_id = safe_payload.get("patient_id")
            if (
                isinstance(patient_id, bool)
                or not isinstance(patient_id, int)
                or bind.execute(
                    sa.text("SELECT 1 FROM users WHERE id = :id"),
                    {"id": patient_id},
                ).first() is None
            ):
                patient_id = None
                safe_payload.pop("patient_id", None)
            bind.execute(sa.text(
                "UPDATE hardware_traffic SET patient_id = :patient_id, payload = :payload WHERE id = :id"
            ), {
                "patient_id": patient_id,
                "payload": json.dumps(safe_payload, separators=(",", ":")),
                "id": row["id"],
            })


def downgrade():
    raise RuntimeError("This data-preserving migration cannot be downgraded safely.")
