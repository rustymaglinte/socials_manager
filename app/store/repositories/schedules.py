"""Queuing an approved post, claiming it, and recording what happened.

The publisher's whole interface to the database. Three things happen here and
the order matters more than any of them individually:

1. `claim_due` marks rows PUBLISHING and commits. Until that commit lands, no
   other worker can see the claim.
2. The publisher calls the platform -- *outside* any transaction opened here.
3. `mark_published` / `mark_failed` records the outcome.

Every state change goes through `app.domain.states.advance_*` rather than
assigning a column, so an illegal transition raises instead of being written.
The one exception is the claim itself, which is a set-based UPDATE and cannot
call a per-row function; its WHERE clause is restricted to
CLAIMABLE_SCHEDULE_STATES, which is the same rule expressed in SQL.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import (
    CLAIMABLE_SCHEDULE_STATES,
    ScheduleState,
    Verdict,
    advance_schedule,
)
from app.store.models import Approval, ScheduledPost

logger = logging.getLogger(__name__)

# FR-14's dial. Five attempts over roughly an hour and a quarter before an
# operator has to look at it, which is long enough to ride out a Meta outage and
# short enough that a genuinely broken post is not still retrying tomorrow.
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600


@dataclass(frozen=True)
class DuePost:
    """One claimed post, flattened out of the ORM.

    A plain value, not a ScheduledPost, and that is deliberate. The publisher
    holds this across an HTTP call to a platform; an ORM instance carried over
    that boundary is either attached to a session held open for the duration
    (the thing app.store.engine exists to warn against) or detached and liable
    to raise on the next attribute read.

    `body` comes from the *approval*, never from the variant -- an `edited`
    verdict replaced the text, and a variant edited again afterwards must not
    inherit the approval that was given for what it used to say.
    """

    id: uuid.UUID
    brand_slug: str
    platform: str
    body: str
    attempts: int


async def schedule_variant(
    session: AsyncSession,
    *,
    brand_slug: str,
    variant_id: uuid.UUID,
    approval_id: uuid.UUID,
    approval_decision: Verdict,
    platform: str,
    scheduled_for: datetime | None = None,
) -> ScheduledPost:
    """Queue an approved variant for publishing.

    `approval_decision` travels with `approval_id` because the foreign key is
    composite: the database will not accept the pair unless that approval really
    does carry that decision, and the CHECK beside it will not accept a decision
    that is not an approval (C-1, FR-8). Passing the two separately is what lets
    the database do the checking instead of this function.

    `scheduled_for` defaults to now. "Now" is a real schedule, not a special
    case: the worker still picks it up on its own poll, which is what keeps
    FR-12 true -- publishing does not ride on the conversation that approved it.
    """
    post = ScheduledPost(
        brand_slug=brand_slug,
        variant_id=variant_id,
        approval_id=approval_id,
        approval_decision=approval_decision,
        platform=platform,
        scheduled_for=scheduled_for or datetime.now(UTC),
        state=ScheduleState.SCHEDULED,
    )
    session.add(post)
    await session.flush()
    return post


async def claim_due(
    session: AsyncSession,
    *,
    worker: str,
    limit: int = 10,
    brand_slug: str | None = None,
    now: datetime | None = None,
) -> Sequence[DuePost]:
    """Take ownership of up to `limit` posts that are due, and return them.

    `FOR UPDATE SKIP LOCKED` is what makes a second worker safe to start: rows
    another transaction has locked are stepped over rather than waited for, so
    two publishers divide the queue instead of serialising on it.

    `attempts` increments here, at claim time, not on failure. A process killed
    between the platform call and the result must not look like it never tried
    -- that is the difference between a post that retries forever and one that
    eventually dead-letters.
    """
    now = now or datetime.now(UTC)

    due = (
        select(ScheduledPost.id)
        .where(
            ScheduledPost.state.in_(CLAIMABLE_SCHEDULE_STATES),
            ScheduledPost.scheduled_for <= now,
            # Set by mark_failed when backing off. Null for a post that has
            # never failed, which is the common case.
            (ScheduledPost.next_attempt_at.is_(None))
            | (ScheduledPost.next_attempt_at <= now),
        )
        .order_by(ScheduledPost.scheduled_for)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if brand_slug is not None:
        due = due.where(ScheduledPost.brand_slug == brand_slug)

    claimed = (
        await session.execute(
            update(ScheduledPost)
            .where(ScheduledPost.id.in_(due))
            .values(
                state=ScheduleState.PUBLISHING,
                claimed_at=now,
                claimed_by=worker,
                attempts=ScheduledPost.attempts + 1,
            )
            .returning(
                ScheduledPost.id,
                ScheduledPost.brand_slug,
                ScheduledPost.platform,
                ScheduledPost.approval_id,
                ScheduledPost.attempts,
            )
        )
    ).all()
    if not claimed:
        return []

    # The text that was actually approved, fetched in one go rather than per
    # row. See DuePost.body on why this is not the variant's body.
    bodies: dict[uuid.UUID, str | None] = {
        row.id: row.approved_body
        for row in (
            await session.execute(
                select(Approval.id, Approval.approved_body).where(
                    Approval.id.in_([row.approval_id for row in claimed])
                )
            )
        ).all()
    }

    posts = [
        DuePost(
            id=row.id,
            brand_slug=row.brand_slug,
            platform=row.platform,
            body=bodies.get(row.approval_id) or "",
            attempts=row.attempts,
        )
        for row in claimed
    ]
    logger.info("Claimed %d post(s) as %s", len(posts), worker)
    return posts


async def _load(session: AsyncSession, post_id: uuid.UUID) -> ScheduledPost:
    post = await session.get(ScheduledPost, post_id, with_for_update=True)
    if post is None:
        raise LookupError(f"No scheduled post {post_id}")
    return post


async def mark_published(
    session: AsyncSession,
    post_id: uuid.UUID,
    *,
    external_id: str,
    url: str | None = None,
) -> None:
    """Record that it went up. PUBLISHING -> PUBLISHED.

    `external_id` is not decoration: the unique index on (platform,
    external_id) means that if a retry ever did land a second copy, the second
    row cannot record its id and the duplicate surfaces as an integrity error
    rather than as a post nobody notices twice.
    """
    post = await _load(session, post_id)
    post.state = advance_schedule(post.state, ScheduleState.PUBLISHED)
    post.external_id = external_id
    post.external_url = url
    post.last_error = None
    post.next_attempt_at = None
    # Flushed here rather than left to the caller's commit: a repository
    # that only mutates the identity map leaves the row unchanged for
    # anything that re-reads it in the same transaction, and the unique
    # index on (platform, external_id) cannot object to a duplicate that
    # has not reached the database yet.
    await session.flush()


def backoff_for(attempts: int) -> timedelta:
    """Exponential, capped. First retry a minute out, then 2, 4, 8...

    Capped because the point of a long wait is to outlast a platform outage,
    and past an hour a human wants to know rather than wait longer.
    """
    seconds = min(
        BASE_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS
    )
    return timedelta(seconds=seconds)


async def mark_failed(
    session: AsyncSession,
    post_id: uuid.UUID,
    *,
    error: str,
    retryable: bool,
    max_attempts: int = MAX_ATTEMPTS,
    now: datetime | None = None,
) -> ScheduleState:
    """Record a failed publish, and decide whether to try again. FR-14.

    Always PUBLISHING -> FAILED first, then out of FAILED to either SCHEDULED
    (back off and retry) or DEAD_LETTER (stop and tell someone). Two hops rather
    than one, because that is what the lifecycle allows -- and the reason it is
    shaped that way is that there is no edge from PUBLISHING back to SCHEDULED:
    a claimed row must pass through FAILED, where something can check whether
    the post actually landed, before it is ever eligible to be sent again.

    `retryable` comes from the adapter's own classification of the platform's
    error code. An expired token or a message the platform refused fails
    identically on the next attempt, so retrying only delays the operator
    finding out.
    """
    now = now or datetime.now(UTC)

    post = await _load(session, post_id)
    post.state = advance_schedule(post.state, ScheduleState.FAILED)
    post.last_error = error

    exhausted = post.attempts >= max_attempts
    if retryable and not exhausted:
        post.state = advance_schedule(post.state, ScheduleState.SCHEDULED)
        post.next_attempt_at = now + backoff_for(post.attempts)
        logger.warning(
            "Publish of %s failed (attempt %d/%d), retrying at %s: %s",
            post_id,
            post.attempts,
            max_attempts,
            post.next_attempt_at,
            error,
        )
    else:
        post.state = advance_schedule(post.state, ScheduleState.DEAD_LETTER)
        post.next_attempt_at = None
        logger.error(
            "Dead-lettered %s after %d attempt(s) (%s): %s",
            post_id,
            post.attempts,
            "not retryable" if not retryable else "attempts exhausted",
            error,
        )

    # See mark_published: the new state has to reach the database, not just
    # the identity map, or the next claim query still sees the old one.
    await session.flush()
    return post.state


async def release_stale_claims(
    session: AsyncSession, *, older_than: timedelta, now: datetime | None = None
) -> int:
    """Fail rows left in PUBLISHING by a worker that died holding them.

    Fails them; does not requeue them. Handing a claimed row back to the queue
    is the obvious recovery and it is how you double-post -- nothing here can
    tell a request that never reached the platform from one whose response was
    lost. FAILED is where a human, or a reconciliation against the platform's
    own post list, decides which of those it was.
    """
    now = now or datetime.now(UTC)
    cutoff = now - older_than

    released = (
        await session.execute(
            update(ScheduledPost)
            .where(
                ScheduledPost.state == ScheduleState.PUBLISHING,
                ScheduledPost.claimed_at < cutoff,
            )
            .values(
                state=ScheduleState.FAILED,
                last_error=(
                    f"Worker stopped responding while publishing; claim went "
                    f"stale after {older_than}. Check the platform before "
                    f"retrying -- the post may have gone up."
                ),
            )
            .returning(ScheduledPost.id)
        )
    ).all()

    if released:
        logger.error(
            "Released %d stale claim(s) to FAILED; verify before retrying",
            len(released),
        )
    return len(released)
