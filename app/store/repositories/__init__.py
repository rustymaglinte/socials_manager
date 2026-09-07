"""Brand-scoped reads and writes. The only code that builds a query.

Split by the thing being written rather than by the caller writing it, so the
publisher and the agent share one definition of what "claim a due post" means.

Import from this package, not from the submodules.
"""

from app.store.repositories.brands import known_slugs, sync_brands
from app.store.repositories.schedules import (
    MAX_ATTEMPTS,
    DuePost,
    backoff_for,
    claim_due,
    mark_failed,
    mark_published,
    release_stale_claims,
    schedule_variant,
)

__all__ = [
    "MAX_ATTEMPTS",
    "DuePost",
    "backoff_for",
    "claim_due",
    "known_slugs",
    "mark_failed",
    "mark_published",
    "release_stale_claims",
    "schedule_variant",
    "sync_brands",
]
