"""app.transports.slack_approval.approval -- posting a draft and awaiting a human.

The coroutine the whole design hangs off: it suspends inside the agent's tool
call and does not come back until somebody clicks, or until nobody does. Its
handlers were tested and its registry was tested; this, the part that joins
them, was not.

Three things it must get right, none of which the pieces either side can check:

- **The graphic goes up before the buttons do.** A reviewer must not be able to
  approve a post whose image is still uploading (C-1, FR-9).
- **An upload that failed is reported as failed.** `image_shown` is what the
  caller uses to decide whether to publish the card at all, so a hopeful `True`
  there is a graphic nobody approved going out on a live Page.
- **A timeout settles the message.** Otherwise the draft keeps its buttons
  forever and a click hours later resolves nothing, silently.

No Slack: `app.client` is replaced with a recorder. The wait is real asyncio,
which is the point of the last group -- the verdict arrives from a different
task, exactly as it does in production.
"""

import asyncio

import pytest

from app.transports.slack_approval import approval
from app.transports.slack_approval.pending import PendingRegistry


class FakeSlack:
    """Records what would have gone to Slack, in the order it was sent."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.uploads: list[dict] = []
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.upload_fails = False

    async def files_upload_v2(self, **kwargs):
        self.calls.append("upload")
        if self.upload_fails:
            raise RuntimeError("Slack said no")
        self.uploads.append(kwargs)

    async def chat_postMessage(self, **kwargs):
        self.calls.append("post")
        self.posts.append(kwargs)
        return {"channel": kwargs["channel"], "ts": "1234.5678"}

    async def chat_update(self, **kwargs):
        self.calls.append("update")
        self.updates.append(kwargs)


@pytest.fixture
def slack(monkeypatch) -> FakeSlack:
    from types import SimpleNamespace

    recorder = FakeSlack()
    monkeypatch.setattr(approval, "app", SimpleNamespace(client=recorder))

    async def already_connected() -> None:
        """`request_approval` opens the listener on its way in; there is no
        socket to open here and no workspace to open it against."""

    monkeypatch.setattr(approval, "start_listener", already_connected)
    monkeypatch.setattr(approval, "channel_for", lambda slug: f"C0{slug.upper()}")
    return recorder


@pytest.fixture
def registry(monkeypatch) -> PendingRegistry:
    fresh = PendingRegistry()
    monkeypatch.setattr(approval, "registry", fresh)
    return fresh


async def decide(registry: PendingRegistry, decision: str, **kwargs) -> None:
    """Answer whichever request is open, from the outside, once there is one.

    A separate task on the same loop, because that is what a Slack handler is:
    the waiter cannot resolve its own request.
    """
    for _ in range(200):
        if registry._requests:
            request_id = next(iter(registry._requests))
            registry.resolve(request_id, decision, "U123", **kwargs)
            return
        await asyncio.sleep(0.001)
    raise AssertionError("no request was ever opened")


# --- the ordinary verdicts ----------------------------------------------------


async def test_an_approval_comes_back_with_who_gave_it(slack, registry):
    """FR-8 needs the identity, not just the answer."""
    waiting = asyncio.create_task(
        approval.request_approval("pinoysing", "facebook", "Kumusta!")
    )
    await decide(registry, "approved")
    verdict = await waiting

    assert verdict["decision"] == "approved"
    assert verdict["user"] == "U123"
    assert verdict["content"] == "Kumusta!"


async def test_an_edit_returns_the_reviewers_words_not_the_models(slack, registry):
    """What the publisher sends is what somebody said yes to."""
    waiting = asyncio.create_task(
        approval.request_approval("pinoysing", "facebook", "the model's draft")
    )
    await decide(registry, "edited", content="the reviewer's rewrite")
    verdict = await waiting

    assert verdict["decision"] == "edited"
    assert verdict["content"] == "the reviewer's rewrite"


async def test_a_rejection_carries_its_reason(slack, registry):
    """FR-11's revision loop is answering this string."""
    waiting = asyncio.create_task(
        approval.request_approval("derekt", "facebook", "a draft")
    )
    await decide(registry, "rejected", reason="the hook is generic")
    verdict = await waiting

    assert verdict["decision"] == "rejected"
    assert verdict["reason"] == "the hook is generic"


async def test_the_draft_is_posted_to_the_brands_own_channel(slack, registry):
    """One slug decides both the context and the channel, so they cannot disagree."""
    waiting = asyncio.create_task(
        approval.request_approval("pinoysing", "facebook", "a draft")
    )
    await decide(registry, "approved")
    await waiting

    assert slack.posts[0]["channel"] == "C0PINOYSING"


# --- nobody looked ------------------------------------------------------------


async def test_a_timeout_is_a_verdict_of_its_own(slack, registry):
    """Not an exception and not a rejection: "nobody looked" is a fact about the
    reviewer, and `record_verdict` stores it as one."""
    verdict = await approval.request_approval(
        "pinoysing", "facebook", "a draft", timeout_seconds=0.01
    )

    assert verdict["decision"] == "timeout"
    assert verdict["user"] is None
    assert verdict["content"] == "a draft"


async def test_a_timed_out_draft_loses_its_buttons(slack, registry):
    """Otherwise the message stays live forever and a click hours later resolves
    nothing, with the reviewer told only that it 'already expired'."""
    await approval.request_approval(
        "pinoysing", "facebook", "a draft", timeout_seconds=0.01
    )

    assert slack.updates, "a timed-out request must settle its message"
    settled = slack.updates[0]["blocks"]
    assert "actions" not in [block["type"] for block in settled]
    assert "Timed out" in slack.updates[0]["text"]


async def test_a_timed_out_request_is_forgotten(slack, registry):
    """The registry is process memory; a request nobody closed is a slow leak."""
    await approval.request_approval(
        "pinoysing", "facebook", "a draft", timeout_seconds=0.01
    )

    assert registry._requests == {}


async def test_a_decided_request_is_forgotten_too(slack, registry):
    waiting = asyncio.create_task(
        approval.request_approval("pinoysing", "facebook", "a draft")
    )
    await decide(registry, "approved")
    await waiting

    assert registry._requests == {}


# --- the graphic --------------------------------------------------------------


async def test_the_graphic_goes_up_before_the_buttons_do(slack, registry):
    """C-1: approving a post means approving the image on it.

    Posting the buttons first would leave a window where the draft can be
    approved while its graphic is still uploading -- a yes given to the text
    alone, recorded as a yes to both.
    """
    waiting = asyncio.create_task(
        approval.request_approval(
            "pinoysing", "facebook", "a draft", image=b"\x89PNG-bytes"
        )
    )
    await decide(registry, "approved")
    verdict = await waiting

    assert slack.calls[:2] == ["upload", "post"]
    assert verdict["image_shown"] is True


async def test_an_upload_that_failed_is_reported_as_not_shown(slack, registry):
    """The caller drops the card on `image_shown` being False.

    Reporting True here would publish a graphic nobody was ever shown, on the
    strength of a yes given to the caption -- which is the one thing the gate
    exists to prevent.
    """
    slack.upload_fails = True
    waiting = asyncio.create_task(
        approval.request_approval(
            "pinoysing", "facebook", "a draft", image=b"\x89PNG-bytes"
        )
    )
    await decide(registry, "approved")
    verdict = await waiting

    assert verdict["image_shown"] is False


async def test_a_failed_upload_still_asks_about_the_text(slack, registry):
    """Not fatal: the post is fine, it just goes out without the graphic."""
    slack.upload_fails = True
    waiting = asyncio.create_task(
        approval.request_approval(
            "pinoysing", "facebook", "a draft", image=b"\x89PNG-bytes"
        )
    )
    await decide(registry, "approved")
    verdict = await waiting

    assert verdict["decision"] == "approved"
    assert slack.posts, "the reviewer must still be asked about the caption"


async def test_a_text_post_uploads_nothing(slack, registry):
    waiting = asyncio.create_task(
        approval.request_approval("derekt", "facebook", "a draft")
    )
    await decide(registry, "approved")
    verdict = await waiting

    assert slack.calls == ["post"]
    assert verdict["image_shown"] is False
