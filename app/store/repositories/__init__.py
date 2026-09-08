"""Brand-scoped reads and writes. The only code that builds a query.

Split by the thing being written rather than by the caller writing it, so the
publisher and the agent share one definition of what "claim a due post" means.

Import from this package, not from the submodules.
"""

from app.store.repositories.brands import known_slugs, sync_brands
from app.store.repositories.drafts import (
    RECENT_ANGLES,
    RECENT_TOPICS,
    approving_verdict,
    attach_card,
    next_draft_state,
    open_draft,
    recent_angles,
    recent_topics,
    record_variant,
    record_verdict,
    variant_for,
)
from app.store.repositories.schedules import (
    MAX_ATTEMPTS,
    DuePost,
    backoff_for,
    claim_due,
    mark_failed,
    mark_published,
    release_stale_claims,
    schedule_variant,
    scheduled_for_thread,
)

__all__ = [
    "MAX_ATTEMPTS",
    "RECENT_ANGLES",
    "RECENT_TOPICS",
    "DuePost",
    "approving_verdict",
    "attach_card",
    "backoff_for",
    "claim_due",
    "known_slugs",
    "mark_failed",
    "mark_published",
    "next_draft_state",
    "open_draft",
    "recent_angles",
    "recent_topics",
    "record_variant",
    "record_verdict",
    "release_stale_claims",
    "schedule_variant",
    "scheduled_for_thread",
    "sync_brands",
    "variant_for",
]
