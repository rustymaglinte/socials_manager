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
from datetime import UTC, datetime, timedelta

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
from app.store.models import Approval, Draft, PostMedia, PostVariant, ScheduledPost

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


# How much of a past post is enough to recognise its topic. The list goes into
# the brief, which is a HumanMessage rather than the cached prefix -- so every
# entry is resent on every turn of the run and never cached. A hook or a first
# line identifies "we did Eraserheads already" as well as the whole post does,
# at a tenth of the tokens, which is what makes a useful number of them
# affordable.
TOPIC_MAX_CHARS = 80

# Roughly a week at pinoysing's cadence (5/day, 35/week). Deliberately counted
# in posts rather than days: a brand posting five times a day and one posting
# twice a week need the same "what have I just said", not the same fortnight.
RECENT_TOPICS = 25

# Enough that a run of one angle cannot happen, small enough that it never
# empties a nine-angle catalog. `pick_angle` takes the set of these, so five
# consecutive drafts on one angle exclude one angle, not five.
RECENT_ANGLES = 5

# The window either read looks back over. A brand that goes quiet for a month
# should start clean rather than dragging stale exclusions forward.
RECENT_DAYS = 14


def _topic(media: list[dict] | None, body: str | None) -> str:
    """One past post, short enough to list.

    The card's hook when there was one -- it is already a summary of the post,
    written to be the six words that carry it -- and the opening of the caption
    otherwise. Whitespace is collapsed so a multi-paragraph post contributes one
    line to the brief rather than reshaping it.
    """
    hook = (media[0].get("hook") if media else None) or ""
    text = " ".join((hook.strip() or (body or "")).split())
    if len(text) > TOPIC_MAX_CHARS:
        return text[:TOPIC_MAX_CHARS].rstrip() + "..."
    return text


async def recent_angles(
    session: AsyncSession,
    *,
    brand_slug: str,
    limit: int = RECENT_ANGLES,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """The angles this brand's last few briefs used, newest first.

    Every draft counts, not only the approved ones -- and that is the difference
    from `recent_topics` below. "Do not land on the same angle twice running" is
    true whether or not the reviewer liked the post; a rejection says the writing
    was wrong, not that the angle was already spent.

    These never reach the model. They only shrink the set `pick_angle` draws
    from, which is why the list can be short and why duplicates in it are
    harmless -- the caller takes its set.
    """
    now = now or datetime.now(UTC)
    angles = (
        await session.scalars(
            select(Draft.angle)
            .where(
                Draft.brand_slug == brand_slug,
                Draft.angle.is_not(None),
                Draft.created_at >= now - timedelta(days=RECENT_DAYS),
            )
            .order_by(Draft.created_at.desc())
            .limit(limit)
        )
    ).all()
    # The WHERE already excludes them; repeating it here is for the reader and
    # the type checker, neither of which can see into the SQL.
    return tuple(angle for angle in angles if angle)


async def recent_topics(
    session: AsyncSession,
    *,
    brand_slug: str,
    limit: int = RECENT_TOPICS,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """What this brand has actually posted lately, newest first.

    Anchored on `scheduled_posts` rather than on drafts, and that is not a
    shortcut: a row can only exist there if an *approving* verdict exists, since
    the foreign key is composite onto (approval id, decision) and the CHECK
    beside it admits only APPROVING_VERDICTS. So "approved" needs no predicate
    here -- the schema already guarantees it (C-1).

    Which is also the answer to why rejected drafts are absent. A reviewer who
    rejects an Eraserheads post has said the post was wrong, not the topic;
    excluding the topic would be inferring a judgment nobody made.

    Text comes from the approval, never from the variant, for the same reason
    the publisher reads it there: an edit replaced the words, and what the brand
    actually said is what went out.
    """
    now = now or datetime.now(UTC)

    rows = (
        await session.execute(
            select(PostVariant.media, Approval.approved_body)
            .select_from(ScheduledPost)
            .join(PostVariant, PostVariant.id == ScheduledPost.variant_id)
            .join(Approval, Approval.id == ScheduledPost.approval_id)
            .where(
                ScheduledPost.brand_slug == brand_slug,
                ScheduledPost.created_at >= now - timedelta(days=RECENT_DAYS),
            )
            .order_by(ScheduledPost.created_at.desc())
            .limit(limit)
        )
    ).all()

    # Deduplicated, order preserved: one concept submitted for four platforms is
    # four rows and one topic, and listing it four times would spend the budget
    # on repetition rather than on coverage.
    seen: dict[str, None] = {}
    for media, approved_body in rows:
        topic = _topic(media, approved_body)
        if topic:
            seen.setdefault(topic, None)
    return tuple(seen)


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
