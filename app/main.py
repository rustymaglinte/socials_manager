import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain.messages import HumanMessage
from langgraph.types import Command
from uuid_utils import uuid4

from app.agent.brief import build_brief, pick_angle
from app.domain import brand
from app.domain.brand import load_brand
from app.domain.brand.context import BrandContext, BrandNotFound
from app.domain.brand.loader import all_brands
from app.domain.states import APPROVING_VERDICTS, Verdict
from app.graphics import Card, RenderFailed, render_card, supports_graphics
from app.llm.factory import agent_creation
from app.store.engine import transaction
from app.store.repositories import (
    approving_verdict,
    attach_card,
    open_draft,
    record_variant,
    record_verdict,
    schedule_variant,
    sync_brands,
    variant_for,
)
from app.transports.slack_approval.approval import request_approval
from app.transports.slack_approval.client import start_listener, stop_listener

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
load_dotenv()

# The brand this run is bound to. Hard-coded until the Slack transport drives the
# run and resolves it from the channel the brief arrived in (SPECS D3); either
# way it is decided here, never read back out of the model's tool arguments.
# Both invokes must carry the same thread_id, or the resume can't find the run.
# (thread_id is LangGraph's key for a conversation -- nothing to do with Threads.)

# A rejection with a note sends the draft back for a rewrite. Capped, so a
# reviewer who keeps rejecting can't spin the agent indefinitely.
MAX_REVISIONS = 5


@dataclass(frozen=True)
class Run:
    """What the gate needs to know about the run a tool call belongs to.

    Three facts that are all decided before the model is invoked and none of
    which the model may supply: the brand (SPECS D3), the draft row being filled
    in, and the angle -- which picks the graphic's template, so a post's look is
    a consequence of the brief rather than of anything the model chose.
    """

    brand: BrandContext
    draft_id: uuid.UUID
    angle: str | None = None


async def to_resume_decision(run: Run, action: dict, revisions: list[str]) -> dict:
    """Ask a human about one pending tool call, in the shape the middleware wants.

    Also the only place the content plane is written. The order is the point:
    the variant is recorded before the reviewer is asked, the verdict is
    recorded before the middleware is answered, and the schedule is written in
    the same transaction as the approval that permits it. So a process that dies
    mid-gate leaves a draft somebody can find, not a Slack message with nothing
    behind it -- and an approved post is queued whether or not this run survives
    long enough to say so.

    `run` carries what was decided before the model was invoked -- brand, draft
    row, angle -- none of which the model may supply. `revisions` is the run's
    memory across gate hits, passed in rather than global so a second run in the
    same process starts clean.
    """
    brand = run.brand
    args = action["args"]

    # Platform is what identifies a variant, and it is optional on the tool, so
    # fall back to its default rather than assuming the model sent one. There is
    # no brand argument to read: `brand` decides the channel (SPECS D3).
    platform = args.get("platform", "default")
    content = args["content"]
    hook = (args.get("hook") or "").strip()

    # Rendered before anything is written, because a hook that does not fit is
    # the model's to fix and the cheapest place to find that out is here. The
    # renderer's own sentence goes back as the tool result (FR-7): deterministic
    # code decides what is legible, the model rewrites it (D9).
    card: Card | None = None
    if hook and supports_graphics(brand):
        try:
            card = await render_card(
                brand, angle=run.angle, hook=hook, sub=(args.get("sub") or "").strip()
            )
        except RenderFailed as error:
            logging.warning(
                "Card for %s/%s not rendered: %s", brand.slug, platform, error
            )
            return {
                "type": "reject",
                "message": (
                    f"The post graphic could not be rendered: {error}\n\n"
                    f"Fix the card copy and call submit_for_approval again for "
                    f"{platform}. The caption itself was not the problem."
                ),
            }
    elif hook:
        # Asked for a graphic the brand cannot produce. Not worth a round trip
        # to the model -- the post is fine, it just goes out as text.
        logging.info(
            "Ignoring card copy for %s: brand declares no theme in brand.yaml",
            brand.slug,
        )

    async with transaction() as session:
        # "Has this platform already had its post" used to be a set in this
        # process. It is a query now, so a restarted agent -- or a second one --
        # gets the same answer, and the variant it is asked about is the same
        # row a rewrite updates rather than a second one beside it.
        existing = await variant_for(session, draft_id=run.draft_id, platform=platform)
        cleared = existing is not None and (
            await approving_verdict(session, variant_id=existing.id) is not None
        )
        if existing is not None and cleared:
            # Deliberately left as it is: the approved variant's text must not
            # be overwritten by a submission that is about to be rejected.
            variant_id = existing.id
        else:
            variant_id = (
                await record_variant(
                    session,
                    brand_slug=brand.slug,
                    draft_id=run.draft_id,
                    platform=platform,
                    body=content,
                )
            ).id

    if cleared:
        logging.warning("Refusing second %s post; one was already approved", platform)
        return {
            "type": "reject",
            "message": (
                f"A {platform} post was already approved for this brief. One "
                f"brief produces one post per platform. Report what was "
                f"submitted and stop. Do not call submit_for_approval for "
                f"{platform} again."
            ),
        }

    verdict = await request_approval(
        brand=brand.slug,
        platform=platform,
        content=content,
        image=card.image if card else None,
    )
    decision = Verdict(verdict["decision"])
    approving = decision in APPROVING_VERDICTS

    # C-1, applied to the graphic: a card the reviewer was never shown is a card
    # nobody approved, so it is dropped rather than published on the strength of
    # a yes that was given to the text alone.
    if card is not None and not verdict.get("image_shown"):
        logging.warning(
            "The %s graphic never reached the reviewer; publishing as text",
            platform,
        )
        card = None

    async with transaction() as session:
        approval = await record_verdict(
            session,
            brand_slug=brand.slug,
            variant_id=variant_id,
            decision=decision,
            approver=verdict["user"],
            # The text as approved, which for an edit is the reviewer's
            # rewrite -- `request_approval` has already put it here.
            approved_body=verdict["content"],
            note=verdict.get("reason"),
        )
        if approving:
            # Written only now, in the same breath as the verdict: until this
            # point the bytes were a proposal, and storing them earlier would
            # leave the publisher holding an image no human ever ruled on.
            if card is not None:
                await attach_card(
                    session,
                    brand_slug=brand.slug,
                    variant_id=variant_id,
                    image=card.image,
                    sha256=card.sha256,
                    descriptor=card.descriptor(),
                    content_type=card.content_type,
                )
            # Same transaction as the approval, so the row that permits
            # publishing and the row that requests it commit together. The
            # worker takes it from here; nothing below this line publishes
            # anything (FR-12).
            await schedule_variant(
                session,
                brand_slug=brand.slug,
                variant_id=variant_id,
                approval_id=approval.id,
                approval_decision=approval.decision,
                platform=platform,
            )

    if decision is Verdict.APPROVED:
        return {"type": "approve"}

    if decision is Verdict.EDITED:
        # Return the complete replacement args, not a diff.
        return {
            "type": "edit",
            "edited_action": {
                "name": action["name"],
                "args": {**args, "content": verdict["content"]},
            },
        }

    # Supplying a message REPLACES the middleware's built-in "do not retry"
    # instruction, so whether the model tries again is decided entirely here.
    reason = verdict.get("reason")

    if reason and len(revisions) < MAX_REVISIONS:
        revisions.append(reason)
        logging.info("Revision %d requested: %s", len(revisions), reason)
        return {
            "type": "reject",
            "message": (
                f"A human rejected this draft with the note: {reason}\n\n"
                f"Rewrite the post to address that note, then call "
                f"submit_for_approval again with the improved version for the "
                f"same platform."
            ),
        }

    if reason:
        logging.warning("Revision limit (%d) reached; stopping.", MAX_REVISIONS)

    # No reason given, or out of revisions: end the run rather than re-rolling blind.
    return {
        "type": "reject",
        "message": (
            "A human rejected this draft in Slack and nothing was submitted. "
            "The task is over. Report that it was rejected and stop. Do not call "
            "this tool again."
        ),
    }


async def run(brief: str, brand: BrandContext, angle: str | None = None) -> str:
    """One brief, start to finish. Returns the agent's closing message."""
    # Connect before the agent runs, so the socket is live when the first click
    # lands -- and on this loop, so the listener keeps serving while we await a
    # verdict inside the gate.
    logging.info("Connecting Slack listener...")
    await start_listener()

    # Minted here rather than inside the config literal because the draft row
    # carries it too: it is the only link back from a published post to the
    # conversation that wrote it (SPECS 7.4 -- Slack history is not read back).
    thread_id = f"{brand.slug}-{uuid4()}"
    run_config = {"configurable": {"thread_id": thread_id}}

    async with transaction() as session:
        # brand_slug is a foreign key, so a brand added to brands/ since the
        # last run would otherwise fail every insert that named it. Idempotent
        # (ON CONFLICT DO NOTHING) -- the publisher does the same on boot.
        await sync_brands(session, [brand.slug])
        draft = await open_draft(
            session,
            brand_slug=brand.slug,
            concept=brief,
            angle=angle,
            thread_id=thread_id,
        )
    this_run = Run(brand=brand, draft_id=draft.id, angle=angle)

    # One slug decides both the context the model gets and the channel the draft
    # is shown in, so the two can never disagree (SPECS D3).
    agent = agent_creation(brand)

    # One post per platform is now the store's rule, not this function's -- see
    # `to_resume_decision`. What stays in memory is the revision count, because
    # it bounds this conversation rather than the draft: a reviewer who keeps
    # rejecting should not be able to spin the agent indefinitely.
    revisions: list[str] = []

    logging.info("Sending brief to agent...")
    response = await agent.ainvoke(
        {"messages": [HumanMessage(content=brief)]}, run_config
    )

    # A while, not an if: the agent can hit the gate more than once in a run.
    while "__interrupt__" in response:
        hitl_request = response["__interrupt__"][0].value
        logging.info(
            "Gate hit: %d pending tool call(s) -> %s",
            len(hitl_request["action_requests"]),
            [a["name"] for a in hitl_request["action_requests"]],
        )
        # Sequential, not gathered: each of these puts a prompt in front of the
        # same reviewer, and asking them three questions at once is worse than
        # asking three times.
        decisions = [
            await to_resume_decision(this_run, action, revisions)
            for action in hitl_request["action_requests"]
        ]
        response = await agent.ainvoke(
            Command(resume={"decisions": decisions}), run_config
        )

    return response["messages"][-1].content


def brand_from_argv() -> BrandContext:
    """The brand this run is for, named on the command line.

    Interim: prod resolves the brand from the channel a brief arrives in
    (SPECS D3). This exists so a run can be driven by hand before that lands.
    """
    if len(sys.argv) != 2:
        known = ", ".join(b.slug for b in all_brands())
        raise SystemExit(f"Usage: python -m app.main <brand>\nBrands: {known}")
    try:
        return load_brand(sys.argv[1])
    except BrandNotFound as error:
        raise SystemExit(str(error)) from error


async def serve() -> None:
    """Prod shape: hold the socket open and let Slack start the runs."""
    await start_listener()
    logging.info("Listening. Mention the bot in a brand channel to start a run.")
    try:
        await asyncio.Event().wait()  # forever, until Ctrl+C / SIGTERM
    finally:
        await stop_listener()


async def one_shot(brand: BrandContext) -> None:
    """Dev shape: brief one brand, print the answer, exit."""
    angle = pick_angle(brand)
    logging.info("Briefing %s on angle %r", brand.slug, angle)
    try:
        print(await run(build_brief(brand, angle), brand, angle=angle))
    finally:
        await stop_listener()


async def main() -> None:
    if len(sys.argv) > 1:
        await one_shot(load_brand(sys.argv[1]))
    else:
        await serve()


if __name__ == "__main__":
    asyncio.run(main())
