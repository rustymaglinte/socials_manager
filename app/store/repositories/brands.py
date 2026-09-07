"""Keeping the brands table in step with brands/.

`brands/<slug>/` is the source of truth; this table exists only so `brand_slug`
can be a foreign key rather than free text (see app.store.models.Brand). So
there is one operation -- make sure a row exists for every brand directory --
and deliberately no update: there is nothing to update, because no attribute of
a brand is stored here.

Takes slugs rather than BrandContexts on purpose. The store has no business
reading a brand's voice or cadence, and a signature that accepted the whole
context would invite it to start.
"""

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.store.models import Brand


async def sync_brands(session: AsyncSession, slugs: Iterable[str]) -> Sequence[str]:
    """Ensure a row exists for each slug. Returns the ones newly inserted.

    ON CONFLICT DO NOTHING rather than a read-then-write: two processes starting
    at once (the Slack listener and the publisher, which both do this on boot)
    would otherwise race between the check and the insert and one of them would
    crash on the primary key.

    Nothing is ever deleted here. A brand directory that goes away leaves its
    row behind on purpose -- drafts and published history still point at it, and
    the RESTRICT foreign keys would refuse the delete anyway. Retiring a brand
    is a deliberate act, not a side effect of moving a folder.
    """
    slugs = sorted(set(slugs))
    if not slugs:
        return []

    statement = (
        insert(Brand)
        .values([{"slug": slug} for slug in slugs])
        .on_conflict_do_nothing(index_elements=["slug"])
        .returning(Brand.slug)
    )
    inserted = (await session.execute(statement)).scalars().all()
    return sorted(inserted)


async def known_slugs(session: AsyncSession) -> Sequence[str]:
    """Every brand the database will accept a foreign key to."""
    return (await session.scalars(select(Brand.slug).order_by(Brand.slug))).all()
