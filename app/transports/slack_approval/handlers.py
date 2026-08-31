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

import json

from app.transports.slack_approval import blocks
from app.transports.slack_approval.client import app, update_message
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
