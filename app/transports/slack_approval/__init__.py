"""Slack approval gate.

    from app.transports.slack_approval import request_approval, start_listener

Layout:
    blocks.py    Block Kit builders -- pure, no Slack calls
    pending.py   the rendezvous between the waiting and listening coroutines
    client.py    credentials, brand <-> channel routing, the Socket Mode connection
    handlers.py  button and modal handlers
    approval.py  request_approval() -- the awaitable gate the agent uses

One channel per brand, keyed by slug in .env (SLACK_<SLUG>_CHANNEL_ID). That
mapping is the whole of D3's enforcement at this layer: outbound, the brand
decides the channel; inbound, the channel decides the brand.

`request_approval`, `start_listener`, and `stop_listener` are coroutines: the
listener and the waiter share one event loop, so a ten-minute wait for a human
costs a suspended coroutine rather than a parked thread.
"""

# `handlers` is imported for its side effect: the decorators register the button
# and modal handlers on the app. Without it, buttons post fine and clicks go
# nowhere.
from app.transports.slack_approval import handlers  # noqa: F401
from app.transports.slack_approval.approval import request_approval
from app.transports.slack_approval.client import (
    brand_for_channel_id,
    channel_for,
    start_listener,
    stop_listener,
)

__all__ = [
    "brand_for_channel_id",
    "channel_for",
    "request_approval",
    "start_listener",
    "stop_listener",
]
