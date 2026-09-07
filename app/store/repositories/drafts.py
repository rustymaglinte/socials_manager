"""Writing down what the agent produced and what the human said about it.

The other half of `schedules.py`. That module is the publisher's interface to
the database; this is the agent's -- the rows that have to exist before
`schedule_variant` can be called at all, in the order one run produces them:

    open_draft            the brief arrived
      -> record_variant   a platform's text went to the reviewer
        -> record_verdict a human ruled on it

The thing worth reading twice is `record_variant`'s upsert. `(draft_id,
platform)` is unique, which is the structural form of "one brief produces one
post per platform" -- the rule `app.main` used to keep in a Python set, where it
was a guard the agent could be talked past and which a second process knew
nothing about. A revision after a rejection is therefore not a second row: it is
the same row with new words, so the constraint bounds the run without standing
in the way of FR-11's rewrite loop.

What replaces the set, then, is not the constraint on its own but the constraint
plus `approving_verdict`: "has this platform already cleared review" is a query,
and it answers the same on the next process as on this one.

Every state change goes through `next_draft_state`, which goes through
`app.domain.states.advance_draft` -- so an impossible move raises here rather
than being written and discovered later.
"""

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import (
    APPROVING_VERDICTS,
    DraftState,
    Verdict,
    advance_draft,
)
from app.store.models import Approval, Draft, PostMedia, PostVariant

logger = logging.getLogger(__name__)


def next_draft_state(current: DraftState, to: DraftState) -> DraftState:
    """Where a draft goes when `to` happens to one of its variants.

    Not a replacement for `advance_draft`: it calls it for every hop, so a move
    the lifecycle does not allow still raises. What it adds is the two places a
    draft's path is not a single edge, both of which exist because a verdict is
    per *variant* (see `app.store.models.Approval`) while state is per *draft*.

    - **A rewrite is two hops.** FR-11 sends a rejected draft back for another
      pass, and `app.domain.states` spells that REJECTED -> DRAFT ->
      PENDING_APPROVAL rather than giving REJECTED a direct edge into review.
    - **A draft that has already cleared stays cleared.** A concept submitted
      for four platforms is reviewed four times, and the first approval makes
      the draft APPROVED, which is terminal. The three later reviews are reviews
      of a variant, not of the concept, so they leave the draft where it is
      instead of raising on an edge that does not exist.
    """
    if current is to or current is DraftState.APPROVED:
        return current
    if current is DraftState.REJECTED and to is DraftState.PENDING_APPROVAL:
        return advance_draft(advance_draft(current, DraftState.DRAFT), to)
    return advance_draft(current, to)


async def _move_draft(session: AsyncSession, draft_id: uuid.UUID, to: DraftState) -> None:
    draft = await session.get(Draft, draft_id)
    if draft is None:
        raise LookupError(f"No draft {draft_id}")
    draft.state = next_draft_state(draft.state, to)


async def open_draft(
    session: AsyncSession,
    *,
    brand_slug: str,
    concept: str,
    angle: str | None = None,
    thread_id: str | None = None,
) -> Draft:
    """Start a draft for one brief. The first row of a run.

    `thread_id` is the LangGraph thread the run was minted with, and is the only
    link back from a durable row to the conversation that produced it -- Slack
    history is explicitly not readable back (SPECS 7.4), so without it a
    published post cannot be traced to the exchange that wrote it.
    """
    draft = Draft(
        brand_slug=brand_slug,
        concept=concept,
        angle=angle,
        thread_id=thread_id,
        state=DraftState.DRAFT,
    )
    session.add(draft)
    await session.flush()
    logger.info("Opened draft %s for %s (angle %r)", draft.id, brand_slug, angle)
    return draft


async def variant_for(
    session: AsyncSession, *, draft_id: uuid.UUID, platform: str
) -> PostVariant | None:
    """This draft's variant for one platform, if it has been written yet."""
    return (
        await session.scalars(
            select(PostVariant).where(
                PostVariant.draft_id == draft_id, PostVariant.platform == platform
            )
        )
    ).first()


async def record_variant(
    session: AsyncSession,
    *,
    brand_slug: str,
    draft_id: uuid.UUID,
    platform: str,
    body: str,
) -> PostVariant:
    """Write the text being submitted for review, and open the draft's review.

    An upsert rather than an insert, and the module docstring says why: a
    rewrite after a rejection is the same variant with new words, not a second
    one. ON CONFLICT rather than a read-then-write for the reason it is used in
    `brands.py` -- the check and the insert would otherwise be two statements
    with a gap between them.

    Deliberately does not touch `media`: a revision replaces the words, and the
    descriptors of what should be attached are not what the reviewer objected to.
    """
    statement = (
        insert(PostVariant)
        .values(
            brand_slug=brand_slug, draft_id=draft_id, platform=platform, body=body
        )
        .on_conflict_do_update(
            constraint="uq_variant_draft_platform",
            set_={"body": body, "updated_at": sa.func.now()},
        )
        .returning(PostVariant)
        # The gate reads the variant before writing it, so this row may already
        # be in the identity map holding the previous revision's text. Without
        # this the ORM hands that stale instance back rather than the row the
        # database just returned.
        .execution_options(populate_existing=True)
    )
    variant = (await session.scalars(statement)).one()

    await _move_draft(session, draft_id, DraftState.PENDING_APPROVAL)
    await session.flush()
    return variant


async def attach_card(
    session: AsyncSession,
    *,
    brand_slug: str,
    variant_id: uuid.UUID,
    image: bytes,
    sha256: str,
    descriptor: dict,
    content_type: str = "image/png",
) -> PostMedia:
    """Store the graphic a reviewer approved, and describe it on the variant.

    Two writes, one unit of work, because they are two halves of one fact: the
    bytes go to `post_media` (what the publisher uploads) and the copy that
    produced them to `PostVariant.media` (what the row says is attached). A
    descriptor without bytes would promise an image nobody can send; bytes
    without a descriptor would be an attachment nothing can explain.

    Upserted on `variant_id`, like the variant itself: a rewrite after a
    rejection replaces the card rather than leaving the rejected one behind.

    Deliberately called only once a verdict has approved the post -- see the
    gate. An image the reviewer never saw must not end up in a row the
    publisher is willing to upload.
    """
    statement = (
        insert(PostMedia)
        .values(
            brand_slug=brand_slug,
            variant_id=variant_id,
            image=image,
            content_type=content_type,
            sha256=sha256,
        )
        .on_conflict_do_update(
            constraint="uq_media_variant",
            set_={"image": image, "content_type": content_type, "sha256": sha256},
        )
        .returning(PostMedia)
        .execution_options(populate_existing=True)
    )
    media = (await session.scalars(statement)).one()

    variant = await session.get(PostVariant, variant_id)
    if variant is None:
        raise LookupError(f"No variant {variant_id}")
    variant.media = [descriptor]

    await session.flush()
    logger.info(
        "Attached %s card to variant %s (%d bytes)",
        descriptor.get("template", "?"),
        variant_id,
        len(image),
    )
    return media


async def approving_verdict(
    session: AsyncSession, *, variant_id: uuid.UUID
) -> Approval | None:
    """The approval that let this variant through, if one was ever given.

    The cross-process replacement for `app.main`'s `submitted` set. A platform
    whose variant already carries an approving verdict has had its one post for
    this brief; asking the database means a restarted agent, or a second one,
    knows that too.
    """
    return (
        await session.scalars(
            select(Approval)
            .where(
                Approval.variant_id == variant_id,
                Approval.decision.in_(sorted(APPROVING_VERDICTS)),
            )
            .order_by(Approval.decided_at.desc())
            .limit(1)
        )
    ).first()


async def record_verdict(
    session: AsyncSession,
    *,
    brand_slug: str,
    variant_id: uuid.UUID,
    decision: Verdict,
    approver: str | None = None,
    approved_body: str | None = None,
    note: str | None = None,
) -> Approval:
    """Record what the human said. FR-8's evidence, and the row a schedule needs.

    Written for every verdict, not only the approving ones: a rejection is what
    FR-11's revision loop is answering, and a timeout -- nobody looked -- is a
    fact about the reviewer worth keeping rather than a null to interpret later.

    `approved_body` is snapshotted rather than referenced, and is dropped for a
    non-approving verdict: it is the text the publisher will send, so it must be
    the text somebody said yes to and nothing else.

    The draft follows the variant. An approving verdict makes the concept
    APPROVED; anything else -- including a timeout, where there is no verdict to
    speak of -- leaves it REJECTED, which is the state FR-11 rewrites out of.
    """
    approving = decision in APPROVING_VERDICTS

    # Resolved before the row is built, so a variant that is not there leaves
    # nothing pending in the session for the caller's commit to trip over.
    draft_id = (
        await session.scalars(
            select(PostVariant.draft_id).where(PostVariant.id == variant_id)
        )
    ).first()
    if draft_id is None:
        raise LookupError(f"No variant {variant_id}")

    approval = Approval(
        brand_slug=brand_slug,
        variant_id=variant_id,
        decision=decision,
        approver=approver,
        approved_body=approved_body if approving else None,
        note=note,
    )
    session.add(approval)

    await _move_draft(
        session, draft_id, DraftState.APPROVED if approving else DraftState.REJECTED
    )
    await session.flush()
    logger.info(
        "Recorded %s on variant %s by %s", decision.value, variant_id, approver or "nobody"
    )
    return approval
