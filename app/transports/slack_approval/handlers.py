"""Slack interaction handlers.

Importing this module registers them on the Bolt app -- see __init__.py.

Approve and reject are one interaction each. Edit is two: the button opens a
modal, and the verdict only arrives with the modal submission. So the edit
button handler must NOT wake the waiter; only the view handler does.

Every path ends by rewriting the message: the draft stays visible as a record,
the buttons go away.

Handlers are coroutines because the app is an AsyncApp -- Bolt dispatches them
on the same loop the waiter is suspended on, so `registry.resolve` and the
waiter's wake-up need no cross-thread handoff.
"""

import asyncio
import json
from asyncio.log import logger

from app.domain.brand.context import BrandContext
from app.transports.slack_approval import blocks
from app.transports.slack_approval.client import (
    app,
    brand_for_channel_id,
    channel_for,
    update_message,
)
from app.transports.slack_approval.pending import registry

EXPIRED_SUFFIX = "  _(request already expired)_"


async def _settle(body: dict, decision: str, label: str, client) -> None:
    """Record a terminal verdict and settle the message."""
    request_id = body["actions"][0]["value"]
    user = body["user"]["id"]

    resolved = registry.resolve(request_id, decision, user)

    verdict = f"{label} by <@{user}>"
    if not resolved:
        verdict += EXPIRED_SUFFIX

    # Built from the message Slack just handed back, so the draft survives even
    # for a request the registry has already dropped.
    await update_message(
        client,
        body["container"]["channel_id"],
        body["container"]["message_ts"],
        verdict,
        blocks.settle(body["message"]["blocks"], verdict),
    )


@app.action("approve_request")
async def on_approve(ack, body, client):
    await ack()  # Slack allows 3 seconds; nothing slow goes above this line
    await _settle(body, "approved", ":white_check_mark: Approved", client)


def _metadata(body: dict, request_id: str) -> str:
    """Carry the message coordinates through a modal round trip."""
    return json.dumps(
        {
            "request_id": request_id,
            "channel": body["container"]["channel_id"],
            "ts": body["container"]["message_ts"],
        }
    )


@app.action("reject_request")
async def on_reject(ack, body, client):
    """Open the reason modal. Like edit, this does not wake the waiter."""
    await ack()
    request_id = body["actions"][0]["value"]

    if registry.get(request_id) is None:
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=blocks.notice_modal(
                "Expired", "This request has already been decided or timed out."
            ),
        )
        return

    await client.views_open(
        trigger_id=body["trigger_id"],
        view=blocks.reject_modal(_metadata(body, request_id)),
    )


@app.view("reject_submit")
async def on_reject_submit(ack, body, view, client):
    """The reason arrives here. Blank means stop; anything else asks for a rewrite."""
    await ack()

    metadata = json.loads(view["private_metadata"])
    user = body["user"]["id"]
    reason = (view["state"]["values"]["reason"]["value"]["value"] or "").strip()

    request = registry.get(metadata["request_id"])
    resolved = registry.resolve(
        metadata["request_id"], "rejected", user, reason=reason or None
    )

    verdict = f":x: Rejected by <@{user}>"
    if reason:
        verdict += f"\n> {reason}"
    if not resolved:
        verdict += EXPIRED_SUFFIX

    brand = request.brand if request is not None else "unknown"
    platform = request.platform if request is not None else "draft"
    content = request.content if request is not None else ""
    await update_message(
        client,
        metadata["channel"],
        metadata["ts"],
        f":x: Rejected by <@{user}>",
        blocks.settled_message(brand, platform, content, verdict),
    )


@app.action("edit_request")
async def on_edit(ack, body, client):
    """Open the edit modal. Deliberately does not wake the waiter.

    trigger_id expires in 3 seconds, so views_open has to happen immediately.
    The registry lookup above it is an in-memory read, not I/O.
    """
    await ack()
    request_id = body["actions"][0]["value"]
    request = registry.get(request_id)

    if request is None:
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=blocks.notice_modal(
                "Expired", "This request has already been decided or timed out."
            ),
        )
        return

    # Slack caps a text input at 3,000 characters. Refuse rather than silently
    # truncate, which would save a shortened post.
    if len(request.content) > blocks.MODAL_MAX_CHARS:
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=blocks.notice_modal(
                "Too long to edit",
                f"This draft is {len(request.content):,} characters, over Slack's "
                f"{blocks.MODAL_MAX_CHARS:,}-character limit for an edit box. "
                f"Approve or reject it instead.",
            ),
        )
        return

    await client.views_open(
        trigger_id=body["trigger_id"],
        view=blocks.edit_modal(
            f"{request.brand} · {request.platform}",
            request.content,
            _metadata(body, request_id),
        ),
    )


@app.view("edit_submit")
async def on_edit_submit(ack, body, view, client):
    """The edited text arrives here, not on the button click."""
    await ack()

    metadata = json.loads(view["private_metadata"])
    request_id = metadata["request_id"]
    user = body["user"]["id"]
    edited = view["state"]["values"]["draft"]["content"]["value"]

    # Grab the object before resolving: the waiter wakes on resolve and closes
    # the request, but this reference stays valid.
    request = registry.get(request_id)
    resolved = registry.resolve(request_id, "edited", user, content=edited)

    verdict = f":pencil2: Edited and approved by <@{user}>"
    if not resolved:
        verdict += EXPIRED_SUFFIX

    # A view submission arrives without the message, so rebuild it -- and it has
    # to show the edited text anyway, not what was there before.
    brand = request.brand if request is not None else "unknown"
    platform = request.platform if request is not None else "draft"
    await update_message(
        client,
        metadata["channel"],
        metadata["ts"],
        verdict,
        blocks.settled_message(brand, platform, edited, verdict),
    )


# How much of a failure goes into the channel. Enough to tell a missing table
# from a refused connection from an expired model key, and no more -- the
# traceback is in the log, and a wall of it in Slack buries the sentence that
# matters ("nothing was posted").
ERROR_MAX_CHARS = 300


async def _report_failure(brand: BrandContext, error: Exception) -> None:
    """Tell the channel the run died. Nothing else will.

    `_brief_run` is a fire-and-forget task, so its exception has nowhere to
    propagate to: without this, a run that fell over is indistinguishable in
    Slack from one that nobody started. The operator is looking at the channel,
    not at the bot's log -- and since `run` now writes to the database before it
    writes anything to Slack, the most likely failure is one that happens before
    a single draft appears.

    Says explicitly that approved posts are unaffected, because that is the
    non-obvious part and it is the whole point of D2: the publisher is a
    separate process, and a post that already cleared review is already queued
    in a row this crash cannot touch.

    Never raises. If Slack is what broke, this cannot work either, and letting
    it throw would replace a useful traceback in the log with a useless one.
    """
    detail = f"{type(error).__name__}: {error}".strip()
    if len(detail) > ERROR_MAX_CHARS:
        detail = detail[:ERROR_MAX_CHARS] + "..."

    try:
        await app.client.chat_postMessage(
            channel=channel_for(brand.slug),
            text=(
                f":warning: The *{brand.slug}* run stopped with an error, and "
                f"nothing new was drafted.\n```{detail}```\n"
                f"Anything already approved is still queued -- the publisher is "
                f"a separate process. The full traceback is in the bot's log."
            ),
        )
    except Exception:  # noqa: BLE001 -- see the docstring; never fatal
        logger.exception("Could not tell %s that its run failed", brand.slug)


async def brief_run(brand: BrandContext, approval_timeout: int | None = None) -> None:
    """Brief one brand and see it through, reporting a crash to its channel.

    Public because `app.scheduler` runs the same thing on a timer that a mention
    runs on demand, and the two must fail identically: an unattended 09:00 run
    that dies has nobody watching the log, so the channel is the only place the
    news can land.

    `approval_timeout` is how long the reviewer gets. None keeps `run`'s default,
    which is right for a mention -- somebody just asked for this and is looking
    at Slack. The scheduler passes most of the gap to its next slot instead.
    """
    # Imported here, not at module scope. `app.main` imports this package to
    # reach `request_approval`, so a top-level import would close the loop --
    # and it closes in the one direction that breaks, since `run` is defined
    # further down app.main than the import that pulls this module in. Deferring
    # it costs one dictionary lookup per mention and makes `app.main` importable
    # by something other than `-m`, which is what a test needs.
    from app.main import brief_for, run

    try:
        # The angle comes back named rather than inlined because it is stored on
        # the draft: it decides the graphic's template at publish time, and it
        # is the dimension "which kinds of post work" (FR-4) groups by.
        brief, angle = await brief_for(brand)
        timeout = {} if approval_timeout is None else {"approval_timeout": approval_timeout}
        answer = await run(brief, brand, angle=angle, **timeout)
        logger.info("Run for %s finished: %s", brand.slug, answer)
    except Exception as error:
        logger.exception("Run for %s failed", brand.slug)
        await _report_failure(brand, error)


@app.event("app_mention")
async def on_brief(ack, event):
    await ack()
    brand = brand_for_channel_id(event["channel"])
    asyncio.create_task(brief_run(brand))
