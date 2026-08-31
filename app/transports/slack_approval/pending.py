"""The rendezvous between the waiting coroutine and the listener coroutine.

A decision arrives on Bolt's Socket Mode task, but the coroutine that wants the
answer is suspended inside `request_approval`. They meet here: the waiter opens
a request and awaits its Event, a handler resolves it and sets the Event.

Nothing in this module talks to Slack -- it is plain asyncio, and can be tested
without a workspace.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Literal

Decision = Literal["approved", "edited", "rejected", "timeout"]


@dataclass
class PendingRequest:
    brand: str  # slug; the channel it was posted to is derived from this
    platform: str  # what the draft was written for -- linkedin, x, facebook, youtube
    content: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: Decision | None = None
    user: str | None = None
    reason: str | None = None  # why it was rejected, when the reviewer gave one


class PendingRegistry:
    """Map of request_id -> PendingRequest.

    Unlocked on purpose. The waiter and the handlers are coroutines on one event
    loop, and no method here awaits between reading the dict and writing to it,
    so each is atomic against the others. A lock would only be needed if this
    were shared across threads again.
    """

    def __init__(self) -> None:
        self._requests: dict[str, PendingRequest] = {}

    def open(self, brand: str, platform: str, content: str) -> tuple[str, PendingRequest]:
        request_id = uuid.uuid4().hex
        request = PendingRequest(brand=brand, platform=platform, content=content)
        self._requests[request_id] = request
        return request_id, request

    def get(self, request_id: str) -> PendingRequest | None:
        return self._requests.get(request_id)

    def resolve(
        self,
        request_id: str,
        decision: Decision,
        user: str,
        content: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Record a verdict and wake the waiter.

        Returns False if the request is already gone (decided or timed out),
        which callers use to label the message as expired.
        """
        request = self._requests.get(request_id)
        if request is None:
            return False

        request.decision = decision
        request.user = user
        if content is not None:
            request.content = content
        if reason is not None:
            request.reason = reason

        request.event.set()
        return True

    def close(self, request_id: str) -> PendingRequest | None:
        """Remove the request. Called by the waiter once it stops waiting."""
        return self._requests.pop(request_id, None)


registry = PendingRegistry()
