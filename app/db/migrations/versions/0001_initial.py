"""initial schema: files and features

Revision ID: 0001
Revises:
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

import app.db.types

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=300), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("feature_count", sa.Integer(), nullable=True),
        sa.Column("processed_features", sa.Integer(), nullable=False),
        sa.Column("crs", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("warnings", app.db.types.JSONType, nullable=False),
        sa.Column("stats", app.db.types.JSONType, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", app.db.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.types.UTCDateTime(), nullable=False),
        sa.Column("started_at", app.db.types.UTCDateTime(), nullable=True),
        sa.Column("completed_at", app.db.types.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_files_status", "files", ["status"])
    op.create_index("ix_files_client_status", "files", ["client_id", "status"])

    op.create_table(
        "features",
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("feature_index", sa.Integer(), nullable=False),
        sa.Column("geometry_type", sa.String(length=32), nullable=True),
        sa.Column("geometry_wkb", app.db.types.GeometryBlob, nullable=True),
        sa.Column("properties", app.db.types.JSONType, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("measurement_kind", sa.String(length=8), nullable=True),
        sa.Column("utm_crs", sa.String(length=16), nullable=True),
        sa.Column("utm_value", sa.Float(precision=53), nullable=True),
        sa.Column("geodesic_value", sa.Float(precision=53), nullable=True),
        sa.Column("warnings", app.db.types.JSONType, nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", "feature_index"),
    )


def downgrade() -> None:
    op.drop_table("features")
    op.drop_index("ix_files_client_status", table_name="files")
    op.drop_index("ix_files_status", table_name="files")
    op.drop_table("files")
