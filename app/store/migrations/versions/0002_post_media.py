"""post_media: the approved graphic, as bytes

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-07

Settles the storage half of SPECS Q5 for rendered post cards, and only for
those. The bytes live in Postgres because the two processes that need them are
separate services with ephemeral filesystems -- a path written by the agent is a
path the publisher cannot read, and would not survive a redeploy in any case.
Uploaded or generated media, if it ever arrives, is a different question with a
different answer (object storage); this table is deliberately not general.

Like 0001, a frozen snapshot: nothing here imports app.store.models, so this
records what the schema was on the day it ran rather than following the models
forward.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "post_media",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brand_slug", sa.String(length=64), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        # LargeBinary renders as BYTEA. Postgres moves anything over ~2kB out to
        # TOAST on its own, so a megabyte of PNG costs nothing to a query that
        # does not name this column.
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(["brand_slug"], ["brands.slug"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["variant_id"], ["post_variants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One graphic per variant: a revision replaces the card the way it
        # replaces the words, rather than leaving the old one behind.
        sa.UniqueConstraint("variant_id", name="uq_media_variant"),
    )
    op.create_index(op.f("ix_post_media_brand_slug"), "post_media", ["brand_slug"])


def downgrade() -> None:
    op.drop_index(op.f("ix_post_media_brand_slug"), table_name="post_media")
    op.drop_table("post_media")
