"""The public entry point: post a draft and await a human decision."""

import asyncio
import logging
from typing import Any

from app.transports.slack_approval import blocks
from app.transports.slack_approval.client import (
    DEFAULT_TIMEOUT_SECONDS,
    app,
    channel_for,
    start_listener,
    update_message,
)
from app.transports.slack_approval.pending import registry

logger = logging.getLogger(__name__)


async def _show_graphic(channel: str, brand: str, platform: str, image: bytes) -> bool:
    """Put the rendered card in the channel, above the approval message.

    Uploaded as a file rather than embedded in the approval message's blocks. An
    `image` block needs either a public URL, which these bytes do not have, or a
    `slack_file` reference to an already-uploaded file -- so the upload has to
    happen first either way, and once it has, Slack renders it in the channel by
    itself. Two messages rather than one, in exchange for not depending on a
    file being processed and referenceable within the same breath.

    Returns whether the reviewer can actually see it. False is not fatal here
    but it is decisive upstream: a graphic nobody was shown must not be treated
    as one somebody approved (C-1), so the caller drops it and asks about the
    text alone.
    """
    try:
        await app.client.files_upload_v2(
            channel=channel,
            file=image,
            filename=f"{brand}-{platform}.png",
            title=f"{brand} · {platform}",
        )
    except Exception:
        logger.exception(
            "Could not show the %s graphic for %s; asking about the text alone",
            platform,
            brand,
        )
        return False
    return True


async def request_approval(
    brand: str,
    platform: str,
    content: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    image: bytes | None = None,
) -> dict[str, Any]:
    """Post the draft to `brand`'s channel and suspend until someone decides.

    Awaits rather than blocks: the wait costs a suspended coroutine, not a
    thread, and the Socket Mode listener keeps running on the same loop -- which
    is the only reason the click that ends this wait can arrive at all.

    `brand` is a slug resolved before the model ran (SPECS D3), never something
    the model chose -- it decides which channel the draft is shown in, so a
    wrong value would be a brand-isolation break, not a cosmetic error.

    `image` is the rendered post card, when there is one. It goes up before the
    buttons do, because approving a graphic sight-unseen is the one thing the
    gate exists to prevent -- FR-9 is "the exact final text", and once a post
    carries an image the image is part of what is final.

    Returns {"decision": "approved"|"edited"|"rejected"|"timeout",
             "user": str | None,
             "content": str,     # the edited text when decision == "edited"
             "reason": str | None,   # why, when a rejection came with one
             "image_shown": bool}    # whether the graphic reached the reviewer
    """
    await start_listener()

    channel = channel_for(brand)
    request_id, request = registry.open(brand, platform, content)

    image_shown = False
    if image:
        image_shown = await _show_graphic(channel, brand, platform, image)

    response = await app.client.chat_postMessage(
        channel=channel,
        text=f"Approval needed: {platform} post for {brand}",
        blocks=blocks.approval_message(request_id, brand, platform, content),
    )
    logger.info(
        "Waiting for Slack decision on %s/%s in %s (up to %ss)",
        brand,
        platform,
        channel,
        timeout_seconds,
    )

    try:
        await asyncio.wait_for(request.event.wait(), timeout=timeout_seconds)
        decided = True
    except TimeoutError:
        decided = False
    finally:
        registry.close(request_id)

    if not decided:
        timeout_text = ":hourglass: Timed out — nothing was posted."
        await update_message(
            app.client,
            response["channel"],
            response["ts"],
            timeout_text,
            blocks.settled_message(brand, platform, content, timeout_text),
        )
        return {
            "decision": "timeout",
            "user": None,
            "content": content,
            "reason": None,
            "image_shown": image_shown,
        }

    return {
        "decision": request.decision or "rejected",
        "user": request.user,
        "content": request.content,
        "reason": request.reason,
        "image_shown": image_shown,
    }
