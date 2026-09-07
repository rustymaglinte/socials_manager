"""The claim/settle cycle, against a real Postgres.

Skipped without DATABASE_URL. These are the parts whose correctness is a
property of the database rather than of our Python: `FOR UPDATE SKIP LOCKED`
dividing a queue between workers, a partial unique index refusing a second live
schedule, a composite foreign key refusing an unapproved post. A fake would
agree with whatever we wrote, which is exactly the wrong answer here.

Every test runs inside a transaction that is rolled back, so the database is
left as it was found.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.config import ConfigurationError, settings
from app.domain.states import DraftState, ScheduleState, Verdict
from app.store import Approval, Brand, Draft, PostMedia, PostVariant, ScheduledPost
from app.store.engine import dispose, engine, session_factory
from app.store.repositories import (
    approving_verdict,
    attach_card,
    claim_due,
    known_slugs,
    mark_failed,
    mark_published,
    open_draft,
    record_variant,
    record_verdict,
    release_stale_claims,
    schedule_variant,
    scheduled_for_thread,
    sync_brands,
    variant_for,
)


def _database_configured() -> bool:
    """Ask the config layer, not os.environ.

    DATABASE_URL normally lives in .env, which pydantic-settings reads and
    `os.getenv` does not -- checking the environment directly would skip this
    whole file on the machine it was written on.
    """
    try:
        settings()
    except ConfigurationError:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _database_configured(),
        reason="needs a live Postgres; set DATABASE_URL in .env or the environment",
    ),
]

SLUG = "__test_brand__"
# The concurrency test is the only one that commits, so it gets its own
# brand rather than sharing the fixture's and colliding with it.
CONCURRENT_SLUG = "__test_concurrent__"


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """One engine per test, disposed afterwards.

    Not an optimisation to skip: asyncpg binds a connection to the event loop
    that opened it, and pytest-asyncio gives each test its own loop. A cached
    engine surviving between tests would hand the second one a pool of
    connections belonging to a loop that has already closed.
    """
    yield
    await dispose()


@pytest.fixture
async def session():
    """A session whose work is always rolled back.

    `lock_timeout` so a test that waits on a lock another test's open
    transaction is holding fails in seconds with a legible error, rather
    than hanging the suite until somebody kills it.
    """
    async with session_factory()() as s:
        await s.execute(sa.text("SET lock_timeout = '5s'"))
        yield s
        await s.rollback()


@pytest.fixture
async def variant(session):
    """A brand, a draft and one variant -- the rows a schedule needs to exist."""
    await sync_brands(session, [SLUG])
    draft = Draft(brand_slug=SLUG, concept="a concept", angle="trivia_music")
    session.add(draft)
    await session.flush()
    row = PostVariant(
        brand_slug=SLUG, draft_id=draft.id, platform="facebook", body="draft text"
    )
    session.add(row)
    await session.flush()
    return row


async def _approval(session, variant, decision=Verdict.APPROVED, body="approved text"):
    row = Approval(
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=decision,
        approver="U_REVIEWER",
        approved_body=body if decision in (Verdict.APPROVED, Verdict.EDITED) else None,
    )
    session.add(row)
    await session.flush()
    return row


async def _claim(session, **kwargs):
    """Claim only this file's brand.

    `claim_due` with no brand takes whatever is due anywhere, which would make
    these tests depend on the rest of the database being empty -- and fail
    confusingly the first time somebody leaves a row behind.
    """
    kwargs.setdefault("worker", "w1")
    return await claim_due(session, brand_slug=SLUG, **kwargs)


async def _schedule(session, variant, approval, **kwargs):
    return await schedule_variant(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        approval_id=approval.id,
        approval_decision=approval.decision,
        platform="facebook",
        **kwargs,
    )


# --- seeding ---------------------------------------------------------------


async def test_seeding_is_idempotent(session):
    """Both long-running processes do this on boot; the second must be a no-op."""
    first = await sync_brands(session, [SLUG, "__test_other__"])
    assert set(first) == {SLUG, "__test_other__"}

    again = await sync_brands(session, [SLUG, "__test_other__"])
    assert again == []
    assert SLUG in await known_slugs(session)


async def test_a_draft_cannot_name_a_brand_that_was_never_registered(session):
    """The whole reason the brands table exists: brand_slug is a foreign key,
    so a typo is refused rather than becoming a row nothing can find."""
    session.add(Draft(brand_slug="__never_seeded__", concept="x"))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- claiming --------------------------------------------------------------


async def test_a_due_post_is_claimed_with_the_approved_text(session, variant):
    """Not the variant's body: an edit replaced the text, and the publisher must
    send what the human actually approved."""
    approval = await _approval(session, variant, body="the edited text")
    await _schedule(session, variant, approval)

    claimed = await _claim(session)
    assert len(claimed) == 1
    assert claimed[0].body == "the edited text"
    assert claimed[0].body != variant.body
    assert claimed[0].attempts == 1


async def test_a_post_scheduled_for_later_is_not_claimed(session, variant):
    approval = await _approval(session, variant)
    await _schedule(
        session, variant, approval, scheduled_for=datetime.now(UTC) + timedelta(hours=2)
    )
    assert await _claim(session) == []


async def test_claiming_moves_the_row_to_publishing(session, variant):
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)

    await _claim(session)
    await session.refresh(post)
    assert post.state is ScheduleState.PUBLISHING
    assert post.claimed_by == "w1"
    assert post.claimed_at is not None


async def test_a_claimed_post_is_not_claimed_again(session, variant):
    """Once PUBLISHING it is out of the claimable set, so the same worker
    polling again does not pick up work it is already doing."""
    approval = await _approval(session, variant)
    await _schedule(session, variant, approval)

    assert len(await _claim(session)) == 1
    assert await _claim(session) == []


async def test_two_workers_divide_the_queue_rather_than_serialising():
    """SKIP LOCKED, the reason a second publisher is safe to start.

    The only test here that commits: two transactions cannot see each other's
    uncommitted rows, so the rollback-everything approach the rest of this file
    uses cannot express what this is checking. It cleans up after itself in
    foreign-key order instead -- scheduled_posts holds a RESTRICT reference to
    post_variants, so deleting the drafts first is refused.
    """
    factory = session_factory()

    async def wipe() -> None:
        async with factory() as cleanup:
            for table in (
                ScheduledPost.__table__,
                Approval.__table__,
                PostVariant.__table__,
                Draft.__table__,
            ):
                await cleanup.execute(
                    table.delete().where(table.c.brand_slug == CONCURRENT_SLUG)
                )
            await cleanup.execute(
                Brand.__table__.delete().where(
                    Brand.__table__.c.slug == CONCURRENT_SLUG
                )
            )
            await cleanup.commit()

    await wipe()  # in case a previous run was killed mid-test
    try:
        async with factory() as setup:
            await sync_brands(setup, [CONCURRENT_SLUG])
            draft = Draft(brand_slug=CONCURRENT_SLUG, concept="c")
            setup.add(draft)
            await setup.flush()
            for index in range(4):
                row = PostVariant(
                    brand_slug=CONCURRENT_SLUG,
                    draft_id=draft.id,
                    platform=f"facebook{index}",
                    body="b",
                )
                setup.add(row)
                await setup.flush()
                approval = Approval(
                    brand_slug=CONCURRENT_SLUG,
                    variant_id=row.id,
                    decision=Verdict.APPROVED,
                    approver="U",
                    approved_body="text",
                )
                setup.add(approval)
                await setup.flush()
                await schedule_variant(
                    setup,
                    brand_slug=CONCURRENT_SLUG,
                    variant_id=row.id,
                    approval_id=approval.id,
                    approval_decision=approval.decision,
                    platform=f"facebook{index}",
                )
            await setup.commit()

        async with factory() as one, factory() as two:
            await one.execute(sa.text("SET lock_timeout = '5s'"))
            await two.execute(sa.text("SET lock_timeout = '5s'"))

            first = await claim_due(one, worker="w1", limit=2)
            # w1 holds its rows uncommitted. Without SKIP LOCKED this call
            # blocks until w1 commits; with it, w2 steps over them.
            second = await claim_due(two, worker="w2", limit=2)

            assert len(first) == 2
            assert len(second) == 2, "second worker blocked instead of skipping"
            assert not {p.id for p in first} & {p.id for p in second}

            await one.rollback()
            await two.rollback()
    finally:
        await wipe()


# --- settling --------------------------------------------------------------


async def test_a_published_post_records_its_platform_id(session, variant):
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)
    await _claim(session)

    await mark_published(session, post.id, external_id="67890_1", url="https://x/1")
    await session.refresh(post)
    assert post.state is ScheduleState.PUBLISHED
    assert post.external_id == "67890_1"
    assert post.last_error is None


async def test_a_retryable_failure_goes_back_to_the_queue_with_a_delay(
    session, variant
):
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)
    await _claim(session)

    state = await mark_failed(session, post.id, error="rate limited", retryable=True)
    assert state is ScheduleState.SCHEDULED

    await session.refresh(post)
    assert post.next_attempt_at is not None
    assert post.last_error == "rate limited"
    # Not claimable until the backoff elapses.
    assert await _claim(session) == []


async def test_a_non_retryable_failure_dead_letters_immediately(session, variant):
    """An expired token fails identically next time; retrying only delays the
    operator finding out."""
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)
    await _claim(session)

    state = await mark_failed(session, post.id, error="token expired", retryable=False)
    assert state is ScheduleState.DEAD_LETTER


async def test_retries_are_exhausted_rather_than_endless(session, variant):
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)

    for _ in range(3):
        await _claim(session, now=datetime.now(UTC) + timedelta(days=1))
        state = await mark_failed(
            session, post.id, error="down", retryable=True, max_attempts=3
        )
    assert state is ScheduleState.DEAD_LETTER
    await session.refresh(post)
    assert post.attempts == 3


# --- the guarantees --------------------------------------------------------


async def test_a_rejected_variant_cannot_be_scheduled(session, variant):
    """C-1 and FR-8. Enforced by the composite foreign key and the CHECK beside
    it, not by anything this repository remembers to do."""
    rejected = await _approval(session, variant, decision=Verdict.REJECTED)
    with pytest.raises(IntegrityError):
        await _schedule(session, variant, rejected)


async def test_one_live_schedule_per_variant(session, variant):
    """FR-13, as a partial unique index."""
    approval = await _approval(session, variant)
    await _schedule(session, variant, approval)
    with pytest.raises(IntegrityError):
        await _schedule(session, variant, approval)


async def test_a_stale_claim_fails_rather_than_returning_to_the_queue(
    session, variant
):
    """The recovery that would double-post is the one deliberately not taken."""
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)
    await claim_due(session, worker="dead-worker")

    # Pretend the claim is old.
    post.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()

    assert await release_stale_claims(session, older_than=timedelta(minutes=15)) == 1
    await session.refresh(post)
    assert post.state is ScheduleState.FAILED
    assert post.state is not ScheduleState.SCHEDULED
    assert "may have gone up" in post.last_error


async def test_a_fresh_claim_is_left_alone(session, variant):
    """Well clear of the adapter's 90-second upload timeout: a slow publish
    must never be mistaken for a dead worker."""
    approval = await _approval(session, variant)
    post = await _schedule(session, variant, approval)
    await _claim(session)

    assert await release_stale_claims(session, older_than=timedelta(minutes=15)) == 0
    await session.refresh(post)
    assert post.state is ScheduleState.PUBLISHING


async def test_settling_a_post_that_is_gone_is_an_error_not_a_silent_no_op(session):
    with pytest.raises(LookupError):
        await mark_published(session, uuid.uuid4(), external_id="x")


async def test_the_engine_is_reused_across_calls():
    assert engine() is engine()


# --- what the agent writes -------------------------------------------------
#
# The other end of the same loop: everything above assumes a draft, a variant
# and an approval already exist. These are the calls that put them there, and
# what is being checked is that the gate's memory really has moved out of
# `app.main`'s process and into rows another process can read.

THREAD = "__test_brand__-thread-1"


@pytest.fixture
async def draft(session):
    await sync_brands(session, [SLUG])
    return await open_draft(
        session,
        brand_slug=SLUG,
        concept="a concept someone asked for",
        angle="trivia_music",
        thread_id=THREAD,
    )


async def _submit(session, draft, platform="facebook", body="first attempt"):
    return await record_variant(
        session,
        brand_slug=SLUG,
        draft_id=draft.id,
        platform=platform,
        body=body,
    )


async def test_a_draft_starts_inert_and_enters_review_when_submitted(session, draft):
    assert draft.state is DraftState.DRAFT
    assert draft.thread_id == THREAD

    await _submit(session, draft)
    assert draft.state is DraftState.PENDING_APPROVAL


async def test_a_revision_rewrites_the_variant_rather_than_adding_one(session, draft):
    """FR-11's rewrite loop against FR-13's one-post-per-platform rule. The
    unique constraint on (draft_id, platform) is what makes the second
    submission an update -- if it inserted, the rule would be a Python
    convention again."""
    first = await _submit(session, draft, body="first attempt")
    await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=first.id,
        decision=Verdict.REJECTED,
        approver="U_REVIEWER",
        note="too long",
    )
    second = await _submit(session, draft, body="tightened up")

    assert second.id == first.id
    assert second.body == "tightened up"
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(PostVariant)
            .where(PostVariant.draft_id == draft.id)
        )
    ) == 1
    # And the draft is back in review rather than stuck in REJECTED.
    assert draft.state is DraftState.PENDING_APPROVAL


async def test_a_second_platform_gets_its_own_variant(session, draft):
    facebook = await _submit(session, draft, platform="facebook")
    linkedin = await _submit(session, draft, platform="linkedin", body="for linkedin")
    assert facebook.id != linkedin.id


async def test_an_approved_platform_is_visible_to_the_next_process(session, draft):
    """What replaced `app.main`'s `submitted` set. The set answered only inside
    the process that held it; this answers from the row."""
    variant = await _submit(session, draft)
    assert await approving_verdict(session, variant_id=variant.id) is None

    await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=Verdict.APPROVED,
        approver="U_REVIEWER",
        approved_body="first attempt",
    )

    found = await approving_verdict(session, variant_id=variant.id)
    assert found is not None
    assert found.approver == "U_REVIEWER"
    assert draft.state is DraftState.APPROVED
    # And the same question, asked the way the gate asks it.
    again = await variant_for(session, draft_id=draft.id, platform="facebook")
    assert await approving_verdict(session, variant_id=again.id) is not None


async def test_a_rejection_is_recorded_without_the_text_being_approved(session, draft):
    """A rejection is evidence too, and `approved_body` must not survive it --
    it is what the publisher sends, so it has to be text somebody said yes to."""
    variant = await _submit(session, draft)
    approval = await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=Verdict.REJECTED,
        approver="U_REVIEWER",
        approved_body="first attempt",
        note="wrong tone",
    )
    assert approval.approved_body is None
    assert approval.note == "wrong tone"
    assert await approving_verdict(session, variant_id=variant.id) is None
    assert draft.state is DraftState.REJECTED


async def test_nobody_looking_is_recorded_as_a_verdict(session, draft):
    """`timeout` is a fact about the reviewer, not a null to interpret later."""
    variant = await _submit(session, draft)
    approval = await record_verdict(
        session, brand_slug=SLUG, variant_id=variant.id, decision=Verdict.TIMEOUT
    )
    assert approval.decision is Verdict.TIMEOUT
    assert approval.approver is None
    assert await approving_verdict(session, variant_id=variant.id) is None


async def test_an_edit_is_an_approval_and_carries_the_reviewers_words(session, draft):
    variant = await _submit(session, draft, body="what the model wrote")
    await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=Verdict.EDITED,
        approver="U_REVIEWER",
        approved_body="what the human wrote instead",
    )

    approval = await approving_verdict(session, variant_id=variant.id)
    assert approval.approved_body == "what the human wrote instead"
    # The variant keeps what the model wrote; the publisher reads the approval.
    assert variant.body == "what the model wrote"


async def test_the_whole_loop_ends_in_a_row_the_publisher_will_claim(session, draft):
    """Draft -> variant -> approval -> schedule, then the publisher's own claim
    query picks it up. The join `app.main` and the worker meet at."""
    variant = await _submit(session, draft, body="ready to go")
    approval = await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=Verdict.APPROVED,
        approver="U_REVIEWER",
        approved_body="ready to go",
    )
    await schedule_variant(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        approval_id=approval.id,
        approval_decision=approval.decision,
        platform="facebook",
    )

    claimed = await _claim(session)
    assert len(claimed) == 1
    assert claimed[0].body == "ready to go"


async def test_a_queued_post_can_be_found_from_the_conversation_that_made_it(
    session, draft
):
    """What `submit_for_approval` reports with. The tool knows the LangGraph
    thread and the platform and nothing else."""
    assert (
        await scheduled_for_thread(session, thread_id=THREAD, platform="facebook")
    ) is None

    variant = await _submit(session, draft)
    approval = await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=Verdict.APPROVED,
        approver="U_REVIEWER",
        approved_body="first attempt",
    )
    post = await schedule_variant(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        approval_id=approval.id,
        approval_decision=approval.decision,
        platform="facebook",
    )

    found = await scheduled_for_thread(session, thread_id=THREAD, platform="facebook")
    assert found is not None and found.id == post.id
    # A platform this run never submitted has nothing to report.
    assert (
        await scheduled_for_thread(session, thread_id=THREAD, platform="linkedin")
    ) is None


async def test_a_verdict_on_a_variant_that_is_gone_is_an_error(session):
    with pytest.raises(LookupError):
        await record_verdict(
            session,
            brand_slug=SLUG,
            variant_id=uuid.uuid4(),
            decision=Verdict.APPROVED,
        )


async def test_a_card_is_stored_as_bytes_and_described_on_the_variant(session, draft):
    """The split: `post_media` holds the artefact, `PostVariant.media` says what
    it is. Bytes in Postgres because the agent and the publisher are separate
    services that share a database and nothing else."""
    variant = await _submit(session, draft)
    await attach_card(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        image=b"\x89PNG-first",
        sha256="aaa",
        descriptor={"kind": "card", "template": "spotlight", "hook": "A hook"},
    )

    media = (
        await session.scalars(
            sa.select(PostMedia).where(PostMedia.variant_id == variant.id)
        )
    ).all()
    assert len(media) == 1
    assert media[0].image == b"\x89PNG-first"
    assert media[0].content_type == "image/png"
    assert variant.media[0]["hook"] == "A hook"


async def test_a_second_card_replaces_the_first(session, draft):
    """One graphic per variant, as a unique constraint: a rewrite replaces the
    card the way it replaces the words."""
    variant = await _submit(session, draft)
    for image, digest, hook in (
        (b"\x89PNG-first", "aaa", "Generic hook"),
        (b"\x89PNG-second", "bbb", "Sharper hook"),
    ):
        await attach_card(
            session,
            brand_slug=SLUG,
            variant_id=variant.id,
            image=image,
            sha256=digest,
            descriptor={"kind": "card", "template": "spotlight", "hook": hook},
        )

    media = (
        await session.scalars(
            sa.select(PostMedia).where(PostMedia.variant_id == variant.id)
        )
    ).all()
    assert len(media) == 1
    assert media[0].image == b"\x89PNG-second"
    assert variant.media[0]["hook"] == "Sharper hook"


async def test_a_claimed_post_carries_its_approved_graphic(session, draft):
    """What the publisher gets. Bytes come with the claim rather than being
    fetched later: by then it is out of any transaction and mid-HTTP call."""
    variant = await _submit(session, draft, body="ready to go")
    await attach_card(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        image=b"\x89PNG-approved",
        sha256="ccc",
        descriptor={"kind": "card", "template": "marquee", "hook": "A hook"},
    )
    approval = await record_verdict(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        decision=Verdict.APPROVED,
        approver="U_REVIEWER",
        approved_body="ready to go",
    )
    await schedule_variant(
        session,
        brand_slug=SLUG,
        variant_id=variant.id,
        approval_id=approval.id,
        approval_decision=approval.decision,
        platform="facebook",
    )

    (claimed,) = await _claim(session)
    assert claimed.image == b"\x89PNG-approved"
    assert claimed.body == "ready to go"


async def test_a_text_post_is_claimed_with_no_image(session, variant):
    """The common case, and the one that must not pay for the column."""
    approval = await _approval(session, variant)
    await _schedule(session, variant, approval)

    (claimed,) = await _claim(session)
    assert claimed.image is None
