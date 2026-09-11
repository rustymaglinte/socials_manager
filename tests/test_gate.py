"""`app.main`'s approval gate, against the store it now writes to.

Skipped without DATABASE_URL, and for the same reason as
test_store_repositories.py: what is being checked here is that the run's memory
really did move out of the process. A fake store would remember whatever this
test told it to, which is the thing that already worked.

Slack is stubbed -- `request_approval` is the one call in the gate that needs a
workspace, and what it returns is the only input the gate reacts to. Everything
else in these tests is real: real transactions, real constraints, real
lifecycle.

Unlike the repository tests, these cannot roll back: the gate opens its own
transactions and commits them, which is the property being tested. So this file
owns a brand of its own and deletes its rows in foreign-key order on the way in
and out.
"""

from typing import ClassVar

import pytest
import sqlalchemy as sa

from app import main
from app.config import ConfigurationError, settings
from app.domain.brand.context import PostTheme
from app.domain.states import DraftState, ScheduleState, Verdict
from app.graphics import RenderFailed
from app.store import Approval, Brand, Draft, PostMedia, PostVariant, ScheduledPost
from app.store.engine import dispose, session_factory, transaction
from app.store.repositories import open_draft, sync_brands
from tests.conftest import make_brand


def _database_configured() -> bool:
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

SLUG = "__test_gate__"
BRAND = make_brand(slug=SLUG)

# The same brand, but declaring a palette. `BrandContext.theme` is nullable
# because a brand without one "can still post text" -- derekt is exactly that
# today -- so the two cases need two brands rather than one flag.
THEMED = make_brand(
    slug=SLUG,
    theme=PostTheme(
        ground="#3A3A38",
        accent="#F5D24E",
        muted="#CFCBBD",
        wordmark="TestBrand",
        tagline="a tagline",
        templates={"default": "marquee", "trivia_music": "spotlight"},
    ),
)


@pytest.fixture(autouse=True)
async def clean():
    """One engine per test, and no rows left behind.

    The engine is disposed because asyncpg binds a connection to the loop that
    opened it and pytest-asyncio gives each test its own -- see the same fixture
    in test_store_repositories.py.
    """

    async def wipe():
        async with session_factory()() as session:
            for table in (
                ScheduledPost.__table__,
                Approval.__table__,
                PostVariant.__table__,
                Draft.__table__,
            ):
                await session.execute(table.delete().where(table.c.brand_slug == SLUG))
            await session.execute(
                Brand.__table__.delete().where(Brand.__table__.c.slug == SLUG)
            )
            await session.commit()

    await wipe()  # in case a previous run was killed part-way
    yield
    await wipe()
    await dispose()


@pytest.fixture
async def draft_id():
    async with transaction() as session:
        await sync_brands(session, [SLUG])
        return (
            await open_draft(
                session,
                brand_slug=SLUG,
                concept="a concept",
                angle="trivia_music",
                thread_id=f"{SLUG}-thread",
            )
        ).id


@pytest.fixture
def reviewer(monkeypatch):
    """Stand in for Slack. `reviewer.says(...)` sets the next verdict."""

    class Reviewer:
        verdicts: ClassVar[list[dict]] = []
        seen: ClassVar[list[dict]] = []

        @classmethod
        def says(
            cls,
            decision,
            *,
            content="a post",
            user="U_REVIEWER",
            reason=None,
            image_shown=True,
        ):
            cls.verdicts.append(
                {
                    "decision": decision,
                    "user": user,
                    "content": content,
                    "reason": reason,
                    # Whether the graphic actually reached the channel. True by
                    # default because that is the ordinary case; a test sets it
                    # False to exercise C-1's "not shown, not approved".
                    "image_shown": image_shown,
                }
            )

    async def request_approval(
        *, brand, platform, content, image=None, timeout_seconds=None
    ):
        Reviewer.seen.append(
            {
                "brand": brand,
                "platform": platform,
                "content": content,
                "image": image,
                # How long the reviewer was given. The scheduler stretches this
                # to most of the gap before its next slot, so a draft that
                # appears while nobody is watching is not lost in ten minutes.
                "timeout_seconds": timeout_seconds,
            }
        )
        return Reviewer.verdicts.pop(0)

    monkeypatch.setattr(main, "request_approval", request_approval)
    return Reviewer


@pytest.fixture
def renderer(monkeypatch):
    """Stand in for Chromium. Records what it was asked to draw.

    Playwright launches a real browser per render, which is far too slow and
    too environment-dependent for a test whose subject is the gate. What the
    gate must get right is *which* copy and *which* angle reach the renderer,
    and that a failure to draw comes back to the model rather than escaping.
    """

    class Renderer:
        calls: ClassVar[list[dict]] = []
        fails_with: ClassVar[list[Exception]] = []

        @classmethod
        def will_fail(cls, error):
            cls.fails_with.append(error)

    async def render_card(brand, *, angle, hook, sub=""):
        Renderer.calls.append(
            {"brand": brand.slug, "angle": angle, "hook": hook, "sub": sub}
        )
        if Renderer.fails_with:
            raise Renderer.fails_with.pop(0)
        return main.Card(
            image=b"\x89PNG-rendered",
            template="spotlight",
            hook=hook,
            sub=sub,
            sha256="deadbeef",
        )

    monkeypatch.setattr(main, "render_card", render_card)
    return Renderer


def submission(content="a post", platform="facebook", hook=None, sub=None) -> dict:
    """The shape the HITL middleware hands the gate."""
    args = {"content": content, "platform": platform}
    if hook is not None:
        args["hook"] = hook
    if sub is not None:
        args["sub"] = sub
    return {"name": "submit_for_approval", "args": args}


async def rows(model, **where):
    async with session_factory()() as session:
        return (
            await session.scalars(
                sa.select(model).filter_by(brand_slug=SLUG, **where)
            )
        ).all()


async def gate(
    draft_id, action=None, revisions=None, angle="trivia_music", brand=BRAND
):
    return await main.to_resume_decision(
        main.Run(brand=brand, draft_id=draft_id, angle=angle),
        action or submission(),
        revisions if revisions is not None else [],
    )


# --- what one verdict leaves behind ----------------------------------------


async def test_an_approval_leaves_a_row_the_publisher_can_claim(draft_id, reviewer):
    """The whole point of the change. Before it, an approved post existed only
    as a Slack message and a sentence in a transcript."""
    reviewer.says("approved")
    assert await gate(draft_id) == {"type": "approve"}

    (variant,) = await rows(PostVariant)
    assert variant.body == "a post"
    assert variant.platform == "facebook"

    (approval,) = await rows(Approval)
    assert approval.decision is Verdict.APPROVED
    assert approval.approver == "U_REVIEWER"
    assert approval.approved_body == "a post"

    (scheduled,) = await rows(ScheduledPost)
    assert scheduled.state is ScheduleState.SCHEDULED
    assert scheduled.approval_id == approval.id
    assert scheduled.variant_id == variant.id


async def test_an_edit_schedules_the_reviewers_words_not_the_models(draft_id, reviewer):
    reviewer.says("edited", content="what the human wrote")
    decision = await gate(draft_id, submission(content="what the model wrote"))

    assert decision["type"] == "edit"
    assert decision["edited_action"]["args"]["content"] == "what the human wrote"

    (approval,) = await rows(Approval)
    assert approval.decision is Verdict.EDITED
    assert approval.approved_body == "what the human wrote"
    # The variant records what was submitted; the publisher reads the approval.
    (variant,) = await rows(PostVariant)
    assert variant.body == "what the model wrote"
    assert len(await rows(ScheduledPost)) == 1


async def test_a_rejection_records_the_verdict_and_schedules_nothing(
    draft_id, reviewer
):
    """FR-8 keeps the rejection; C-1 keeps it out of the queue."""
    reviewer.says("rejected", reason=None)
    decision = await gate(draft_id)

    assert decision["type"] == "reject"
    (approval,) = await rows(Approval)
    assert approval.decision is Verdict.REJECTED
    assert approval.approved_body is None
    assert await rows(ScheduledPost) == []


async def test_nobody_looking_is_recorded_too(draft_id, reviewer):
    reviewer.says("timeout", user=None)
    await gate(draft_id)

    (approval,) = await rows(Approval)
    assert approval.decision is Verdict.TIMEOUT
    assert approval.approver is None
    assert await rows(ScheduledPost) == []


# --- what replaced the `submitted` set --------------------------------------


async def test_a_platform_that_already_cleared_is_refused_from_the_row(
    draft_id, reviewer
):
    """The rule used to live in a set held by one coroutine. It is a query now,
    so the second call is refused without the reviewer being asked again -- and
    would be refused just the same by a different process."""
    reviewer.says("approved")
    await gate(draft_id)
    assert len(reviewer.seen) == 1

    decision = await gate(draft_id, submission(content="a second facebook post"))

    assert decision["type"] == "reject"
    assert "already approved" in decision["message"]
    # The reviewer was not troubled a second time...
    assert len(reviewer.seen) == 1
    # ...and the approved text was not overwritten by the refused submission.
    (variant,) = await rows(PostVariant)
    assert variant.body == "a post"
    assert len(await rows(ScheduledPost)) == 1


async def test_the_refusal_survives_this_process_forgetting(draft_id, reviewer):
    """The same assertion made honestly: nothing in memory carries between these
    two calls except the draft id, which is a row."""
    reviewer.says("approved")
    await gate(draft_id)

    await dispose()  # a new engine, a new pool -- as a restarted agent would have

    decision = await gate(draft_id, submission(content="try again"))
    assert decision["type"] == "reject"


async def test_another_platform_is_still_allowed(draft_id, reviewer):
    """One post per platform, not one post per brief."""
    reviewer.says("approved")
    reviewer.says("approved")
    await gate(draft_id, submission(platform="facebook"))
    await gate(draft_id, submission(content="for linkedin", platform="linkedin"))

    assert {variant.platform for variant in await rows(PostVariant)} == {
        "facebook",
        "linkedin",
    }
    assert len(await rows(ScheduledPost)) == 2


# --- the revision loop ------------------------------------------------------


async def test_a_rejection_with_a_note_rewrites_the_same_variant(draft_id, reviewer):
    """FR-11 against FR-13. The rewrite is the same row with new words, so the
    unique constraint bounds the run without blocking the revision."""
    revisions: list[str] = []
    reviewer.says("rejected", reason="too long")
    decision = await gate(draft_id, submission(content="first attempt"), revisions)

    assert decision["type"] == "reject"
    assert "too long" in decision["message"]
    assert revisions == ["too long"]

    reviewer.says("approved", content="tightened up")
    await gate(draft_id, submission(content="tightened up"), revisions)

    variants = await rows(PostVariant)
    assert len(variants) == 1
    assert variants[0].body == "tightened up"
    # Both verdicts are kept: the rejection is what the rewrite answered.
    assert {approval.decision for approval in await rows(Approval)} == {
        Verdict.REJECTED,
        Verdict.APPROVED,
    }
    assert len(await rows(ScheduledPost)) == 1


async def test_a_reviewer_who_keeps_rejecting_cannot_spin_the_agent(
    draft_id, reviewer
):
    revisions: list[str] = []
    for attempt in range(main.MAX_REVISIONS):
        reviewer.says("rejected", reason=f"note {attempt}")
        decision = await gate(draft_id, submission(), revisions)
        assert "Rewrite the post" in decision["message"]

    reviewer.says("rejected", reason="one more")
    decision = await gate(draft_id, submission(), revisions)
    assert "The task is over" in decision["message"]
    assert await rows(ScheduledPost) == []


# --- the draft's own lifecycle ---------------------------------------------


async def test_the_draft_follows_its_variants(draft_id, reviewer):
    async def state() -> DraftState:
        async with session_factory()() as session:
            return (await session.get(Draft, draft_id)).state

    assert await state() is DraftState.DRAFT

    reviewer.says("rejected", reason="not yet")
    await gate(draft_id)
    assert await state() is DraftState.REJECTED

    reviewer.says("approved")
    await gate(draft_id)
    assert await state() is DraftState.APPROVED

    # A second platform is reviewed after the concept already cleared. That
    # review is of a variant, not of the concept, so it must not raise on an
    # edge out of a terminal state.
    reviewer.says("rejected", reason=None)
    await gate(draft_id, submission(platform="linkedin"))
    assert await state() is DraftState.APPROVED


# --- the graphic ------------------------------------------------------------
#
# The card is the one thing in the gate that is produced before the reviewer is
# asked and stored only after they answer. Everything below is about that gap.


async def test_a_hook_becomes_a_card_the_reviewer_is_shown(draft_id, reviewer, renderer):
    """Rendered before the ask, so the bytes go up with the question rather than
    after it. FR-9 is "the exact final text"; once there is an image, the image
    is part of what is final."""
    reviewer.says("approved")
    await gate(
        draft_id, submission(hook="Six words, maybe seven", sub="a supporting line"),
        brand=THEMED,
    )

    assert renderer.calls == [
        {
            "brand": SLUG,
            # The brief's angle picks the template -- not the model.
            "angle": "trivia_music",
            "hook": "Six words, maybe seven",
            "sub": "a supporting line",
        }
    ]
    assert reviewer.seen[0]["image"] == b"\x89PNG-rendered"


async def test_an_approved_card_is_stored_as_the_bytes_that_were_shown(
    draft_id, reviewer, renderer
):
    reviewer.says("approved")
    await gate(draft_id, submission(hook="A hook"), brand=THEMED)

    (media,) = await rows(PostMedia)
    assert media.image == b"\x89PNG-rendered"
    assert media.sha256 == "deadbeef"
    assert media.content_type == "image/png"

    # And the variant describes what is attached, without carrying it.
    (variant,) = await rows(PostVariant)
    assert variant.media == [
        {
            "kind": "card",
            "template": "spotlight",
            "hook": "A hook",
            "sub": "",
            "sha256": "deadbeef",
            "content_type": "image/png",
        }
    ]


async def test_a_rejected_card_is_not_stored(draft_id, reviewer, renderer):
    """The bytes were a proposal until somebody said yes. Storing them anyway
    would leave the publisher holding an image no human ruled on."""
    reviewer.says("rejected", reason=None)
    await gate(draft_id, submission(hook="A hook"), brand=THEMED)

    assert await rows(PostMedia) == []
    assert await rows(ScheduledPost) == []


async def test_a_card_the_reviewer_never_saw_is_dropped(draft_id, reviewer, renderer):
    """C-1 applied to the graphic. Slack refused the upload, so the yes was
    given to the text alone -- and the text alone is what publishes."""
    reviewer.says("approved", image_shown=False)
    await gate(draft_id, submission(hook="A hook"), brand=THEMED)

    assert await rows(PostMedia) == []
    # The post itself still went through; only the graphic was dropped.
    assert len(await rows(ScheduledPost)) == 1
    (variant,) = await rows(PostVariant)
    assert variant.media == []


async def test_a_hook_that_will_not_fit_goes_back_to_the_model(
    draft_id, reviewer, renderer
):
    """FR-7: deterministic code decides what is legible and hands the model a
    structured reason, rather than a human being asked about a broken image."""
    renderer.will_fail(RenderFailed("Hook is 61 characters; 42 is the most that fits"))

    decision = await gate(draft_id, submission(hook="x" * 61), brand=THEMED)

    assert decision["type"] == "reject"
    assert "42 is the most that fits" in decision["message"]
    assert "The caption itself was not the problem" in decision["message"]
    # Nobody was troubled, and nothing was written.
    assert reviewer.seen == []
    assert await rows(PostVariant) == []


async def test_a_brand_with_no_theme_posts_the_text_and_says_nothing(
    draft_id, reviewer, renderer
):
    """derekt declares no palette today. That is a text post, not an error and
    not a guessed set of colours."""
    reviewer.says("approved")
    decision = await gate(draft_id, submission(hook="A hook"), brand=BRAND)

    assert decision == {"type": "approve"}
    assert renderer.calls == []
    assert reviewer.seen[0]["image"] is None
    assert await rows(PostMedia) == []


async def test_a_post_with_no_hook_is_a_text_post(draft_id, reviewer, renderer):
    reviewer.says("approved")
    await gate(draft_id, submission(), brand=THEMED)

    assert renderer.calls == []
    assert reviewer.seen[0]["image"] is None
    assert await rows(PostMedia) == []


async def test_a_revision_replaces_the_card_rather_than_adding_one(
    draft_id, reviewer, renderer
):
    """One graphic per variant, like one variant per platform: the rewrite
    replaces the card the way it replaces the words."""
    reviewer.says("rejected", reason="hook is generic")
    await gate(draft_id, submission(hook="Generic hook"), brand=THEMED, revisions=[])

    reviewer.says("approved")
    await gate(draft_id, submission(hook="Sharper hook"), brand=THEMED)

    (media,) = await rows(PostMedia)
    (variant,) = await rows(PostVariant)
    assert media.variant_id == variant.id
    assert variant.media[0]["hook"] == "Sharper hook"


# --- a renderer that keeps failing --------------------------------------------
#
# FR-7 hands a render failure back to the model as a tool result, and the model
# rewrites the card copy. That loop is right for the failure it was designed
# around -- a hook four words too long, fixed on the second attempt.
#
# It is unbounded, though, and the failure that matters on a new deployment is
# not a long hook: it is a Chromium that cannot start at all, which fails
# identically however the copy is rewritten. `MAX_REVISIONS` bounds a reviewer
# who keeps rejecting; nothing bounded this, so the model rewrote a caption
# against a browser that was never going to run, once per recursion step, until
# LangGraph stopped it.


async def test_repeated_render_failures_stop_asking_the_model_to_retry(
    draft_id, reviewer, renderer
):
    """The cap the reviewer's rejections already have, applied to the renderer."""
    revisions: list[str] = []

    for _ in range(main.MAX_REVISIONS):
        renderer.will_fail(RenderFailed("Chromium failed to launch"))
        decision = await gate(
            draft_id, submission(hook="a hook"), revisions=revisions, brand=THEMED
        )
        assert decision["type"] == "reject"

    renderer.will_fail(RenderFailed("Chromium failed to launch"))
    final = await gate(
        draft_id, submission(hook="a hook"), revisions=revisions, brand=THEMED
    )

    assert final["type"] == "reject"
    assert "again" not in final["message"].lower() or "without" in final["message"].lower()


async def test_the_post_is_salvaged_as_text_rather_than_abandoned(
    draft_id, reviewer, renderer
):
    """A broken renderer is not a reason to lose the caption.

    The words were fine; only the graphic could not be drawn. So the last word
    to the model is "submit this without a hook", not "give up" -- a text post
    is a worse post than an illustrated one and a much better one than nothing.
    """
    revisions = ["r"] * main.MAX_REVISIONS
    renderer.will_fail(RenderFailed("Chromium failed to launch"))

    decision = await gate(
        draft_id, submission(hook="a hook"), revisions=revisions, brand=THEMED
    )

    assert decision["type"] == "reject"
    assert "hook" in decision["message"]
    assert "text" in decision["message"].lower()


async def test_a_render_failure_still_gets_its_first_few_retries(
    draft_id, reviewer, renderer
):
    """The hook that is four words too long must still be fixable.

    Capping at zero would trade a runaway loop for a feature nobody asked to
    lose -- FR-7's whole point is that the model rewrites copy the renderer
    rejected.
    """
    revisions: list[str] = []
    renderer.will_fail(RenderFailed("Hook is 61 characters; 42 is the most that fits"))

    decision = await gate(
        draft_id, submission(hook="x" * 61), revisions=revisions, brand=THEMED
    )

    assert decision["type"] == "reject"
    assert "42 is the most that fits" in decision["message"]
    assert "call submit_for_approval again" in decision["message"]


async def test_a_render_failure_spends_the_same_budget_as_a_rejection(
    draft_id, reviewer, renderer
):
    """One budget, not two. What is being bounded is how many times this run
    may go back to the model, whoever asked it to."""
    revisions: list[str] = []
    renderer.will_fail(RenderFailed("Chromium failed to launch"))

    await gate(draft_id, submission(hook="a hook"), revisions=revisions, brand=THEMED)

    assert len(revisions) == 1
