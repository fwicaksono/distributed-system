"""initial schema: users, tasks, task_results, task_logs

Revision ID: 0001
Revises:
Create Date: 2026-09-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Constraint names go through Base.metadata's naming convention ("ck_%(table)s_%(name)s"),
# so check constraints are named without the prefix here to match the ORM models.
TASK_STATUS = postgresql.ENUM(
    "pending", "queued", "processing", "completed", "failed", name="task_status", create_type=False
)


def upgrade() -> None:
    TASK_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column("status", TASK_STATUS, server_default="pending", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default="5", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_tasks_owner_id_users"),
        sa.CheckConstraint("priority BETWEEN 0 AND 10", name="priority_range"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )
    op.create_index(
        "ix_tasks_owner_created", "tasks",
        ["owner_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_tasks_active_status", "tasks", ["status"],
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )

    op.create_table(
        "task_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_task_results"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE",
                                name="fk_task_results_task_id_tasks"),
        sa.UniqueConstraint("task_id", name="uq_task_results_task_id"),
    )

    op.create_table(
        "task_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_task_logs"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE",
                                name="fk_task_logs_task_id_tasks"),
    )
    op.create_index("ix_task_logs_task_created", "task_logs", ["task_id", "created_at"])

    # updated_at maintained by the database itself, so raw SQL updates can't forget it.
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ LANGUAGE plpgsql;
    """)
    for table in ("users", "tasks"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in ("users", "tasks"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.drop_table("task_logs")
    op.drop_table("task_results")
    op.drop_index("ix_tasks_active_status", table_name="tasks")
    op.drop_index("ix_tasks_owner_created", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("users")
    TASK_STATUS.drop(op.get_bind(), checkfirst=True)
