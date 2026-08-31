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


async def request_approval(
    brand: str,
    platform: str,
    content: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Post the draft to `brand`'s channel and suspend until someone decides.

    Awaits rather than blocks: the wait costs a suspended coroutine, not a
    thread, and the Socket Mode listener keeps running on the same loop -- which
    is the only reason the click that ends this wait can arrive at all.

    `brand` is a slug resolved before the model ran (SPECS D3), never something
    the model chose -- it decides which channel the draft is shown in, so a
    wrong value would be a brand-isolation break, not a cosmetic error.

    Returns {"decision": "approved"|"edited"|"rejected"|"timeout",
             "user": str | None,
             "content": str,     # the edited text when decision == "edited"
             "reason": str | None}   # why, when a rejection came with one
    """
    await start_listener()

    channel = channel_for(brand)
    request_id, request = registry.open(brand, platform, content)

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
        }

    return {
        "decision": request.decision or "rejected",
        "user": request.user,
        "content": request.content,
        "reason": request.reason,
    }
