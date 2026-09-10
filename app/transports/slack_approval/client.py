"""Slack connection: credentials, brand routing, and the Socket Mode listener.

Async throughout: the listener and the coroutine waiting on a verdict share one
event loop, which is what lets `request_approval` await a human for ten minutes
without occupying a thread.
"""

import logging
import os

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.domain.brand import BrandContext, BrandNotFound, all_brands, load_brand

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")  # xoxb- : authenticates API calls
APP_TOKEN = os.getenv("SLACK_APP_TOKEN")  # xapp- : opens the WebSocket

DEFAULT_TIMEOUT_SECONDS = 600

app = AsyncApp(token=BOT_TOKEN)

_handler: AsyncSocketModeHandler | None = None


def channel_env_var(slug: str) -> str:
    """`derekt` -> `SLACK_DEREKT_CHANNEL_ID`.

    Channel ids live in .env rather than brand.yaml: a C0... id is workspace
    state, not brand identity, and it differs between a test workspace and the
    real one. brand.yaml keeps the human-readable `slack_channel` name.
    """
    return f"SLACK_{slug.upper()}_CHANNEL_ID"


def _channel_map() -> dict[str, str]:
    """slug -> channel id, for every brand whose id is set.

    Read from the environment on each call rather than frozen at import, so a
    test can set the variables after this module is imported.
    """
    return {
        brand.slug: channel
        for brand in all_brands()
        if (channel := os.getenv(channel_env_var(brand.slug)))
    }


def channel_for(slug: str) -> str:
    """Outbound direction: which channel this brand's drafts are posted to."""
    channel = os.getenv(channel_env_var(slug))
    if not channel:
        raise BrandNotFound(
            f"No Slack channel for brand {slug!r}: set {channel_env_var(slug)} in .env"
        )
    return channel


def brand_for_channel_id(channel_id: str) -> BrandContext:
    """Inbound direction: the channel an event arrived in decides the brand (SPECS D3).

    Slack events carry a C0... id and .env is keyed by the same id, so this is a
    dict lookup -- no `conversations.info` round trip, and no dependence on a
    channel name that anyone in the workspace can rename.
    """
    for slug, channel in _channel_map().items():
        if channel == channel_id:
            return load_brand(slug)
    raise BrandNotFound(f"No brand bound to Slack channel {channel_id!r}")


async def _verify_channel_names() -> None:
    """Warn when an id in .env is not the channel brand.yaml names.

    A transposed id is the one configuration mistake that breaks brand isolation
    silently -- Derekt drafts land in the personal channel and everything still
    looks fine. One `conversations.info` call per brand at startup catches it.

    Non-fatal: it needs the `channels:read` scope, and a missing scope should not
    stop the bot from running.
    """
    for slug, channel_id in _channel_map().items():
        declared = load_brand(slug).slack_channel
        if not declared:
            continue
        try:
            info = await app.client.conversations_info(channel=channel_id)
            actual = info["channel"]["name"]
        except Exception as error:  # noqa: BLE001 -- diagnostics, never fatal
            logger.warning("Could not verify %s -> %s: %s", slug, channel_id, error)
            continue
        if actual != declared:
            logger.warning(
                "%s points at #%s, but brands/%s/brand.yaml says #%s -- "
                "check %s in .env",
                channel_env_var(slug),
                actual,
                slug,
                declared,
                channel_env_var(slug),
            )


async def start_listener() -> None:
    """Open the Socket Mode connection. Non-blocking, and safe to call twice.

    Uses connect_async() rather than start_async(): the connection runs as a
    background task on the caller's loop, so the agent gets control back and the
    two share the loop from here on.
    """
    global _handler
    if _handler is not None:
        return

    missing = [name for name, value in (
        ("SLACK_BOT_TOKEN", BOT_TOKEN),
        ("SLACK_APP_TOKEN", APP_TOKEN),
    ) if not value]

    # Every brand needs a channel, and the failure to catch here is the one that
    # would otherwise surface hours later, at the moment a draft is ready to post.
    routes = _channel_map()
    missing += [
        channel_env_var(brand.slug)
        for brand in all_brands()
        if brand.slug not in routes
    ]
    if missing:
        raise RuntimeError(f"Missing from .env: {', '.join(missing)}")

    # Every other routing mistake announces itself -- a missing variable is the
    # check above, an unknown channel raises on arrival. Two brands sharing one
    # id does neither: `brand_for_channel_id` scans the map and returns the
    # first match, so one brand quietly answers for the other and its drafts go
    # out in the wrong channel under the wrong voice, with nothing anywhere
    # saying so. That is the brand-isolation break D3 exists to prevent, and it
    # arrives as a duplicated clipboard while somebody pastes opaque C0...
    # strings into a deployment's environment one after another.
    shared: dict[str, list[str]] = {}
    for slug, channel in sorted(routes.items()):
        shared.setdefault(channel, []).append(slug)
    collisions = {
        channel: slugs for channel, slugs in shared.items() if len(slugs) > 1
    }
    if collisions:
        raise RuntimeError(
            "Two brands are bound to one Slack channel, which would post one "
            "brand's drafts as another (SPECS D3). Fix the duplicated id in "
            ".env:\n"
            + "\n".join(
                f"  {channel} <- {', '.join(channel_env_var(s) for s in slugs)}"
                f"  ({', '.join(slugs)})"
                for channel, slugs in sorted(collisions.items())
            )
        )

    handler = AsyncSocketModeHandler(app, APP_TOKEN)
    await handler.connect_async()
    # Assigned only once connected, so a failed attempt can be retried rather
    # than leaving the module believing it has a live socket.
    _handler = handler
    logger.info(
        "Slack listener connected; routing %s",
        ", ".join(f"{slug} -> {channel}" for slug, channel in sorted(routes.items())),
    )
    await _verify_channel_names()


async def stop_listener() -> None:
    """Close the Socket Mode connection. Safe to call without one open.

    Needed now that the socket lives on the same loop as the caller: leaving it
    open makes asyncio.run() complain about pending tasks on the way out.
    """
    global _handler
    if _handler is None:
        return
    try:
        await _handler.close_async()
    except Exception as error:  # noqa: BLE001 -- shutdown, never fatal
        logger.warning("Error closing Slack listener: %s", error)
    finally:
        _handler = None


async def update_message(
    client, channel: str, ts: str, summary: str, message_blocks: list[dict]
) -> None:
    """Rewrite a posted message in place.

    `summary` is the notification and screen-reader fallback; `message_blocks`
    is what renders.
    """
    await client.chat_update(
        channel=channel, ts=ts, text=summary, blocks=message_blocks
    )
