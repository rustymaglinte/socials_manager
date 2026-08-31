"""app.transports.slack_approval.handlers -- what a click actually does.

Bolt is not involved: the handlers are plain coroutines, so they are called
directly with a payload shaped like Slack's and a client that records calls.

The split that these tests exist to protect: approve is one interaction and
resolves immediately, while edit and reject are two -- the button only opens a
modal, and waking the waiter there would approve a draft nobody finished editing.
"""

import json

import pytest

from app.transports.slack_approval import blocks, handlers
from app.transports.slack_approval.pending import PendingRegistry


class FakeClient:
    """Records what would have gone to Slack."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.views: list[dict] = []

    async def chat_update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    async def views_open(self, **kwargs) -> None:
        self.views.append(kwargs)

    @property
    def view(self) -> dict:
        assert len(self.views) == 1, f"expected one modal, got {len(self.views)}"
        return self.views[0]["view"]

    @property
    def update(self) -> dict:
        assert len(self.updates) == 1, f"expected one update, got {len(self.updates)}"
        return self.updates[0]


async def ack() -> None:
    """Slack's 3-second acknowledgement."""


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PendingRegistry:
    """A private registry in place of the process-wide one."""
    fresh = PendingRegistry()
    monkeypatch.setattr(handlers, "registry", fresh)
    return fresh


@pytest.fixture
def slack() -> FakeClient:
    return FakeClient()


def click(request_id: str, message_blocks: list[dict] | None = None) -> dict:
    """A button-click payload, trimmed to the keys the handlers read."""
    return {
        "actions": [{"value": request_id}],
        "user": {"id": "U123"},
        "trigger_id": "trigger-1",
        "container": {"channel_id": "C0DEREKT", "message_ts": "1234.5678"},
        "message": {
            "blocks": message_blocks
            or blocks.approval_message(request_id, "derekt", "linkedin", "the draft")
        },
    }


def submission(request_id: str, state: dict) -> tuple[dict, dict]:
    """A modal-submission payload: (body, view)."""
    body = {"user": {"id": "U123"}}
    view = {
        "private_metadata": json.dumps(
            {"request_id": request_id, "channel": "C0DEREKT", "ts": "1234.5678"}
        ),
        "state": {"values": state},
    }
    return body, view


def header_of(message_blocks: list[dict]) -> str:
    return next(
        block["text"]["text"]
        for block in message_blocks
        if block.get("block_id") == blocks.HEADER_BLOCK_ID
    )


# --- approve -----------------------------------------------------------------


async def test_approve_resolves_the_request_and_settles_the_message(registry, slack):
    request_id, request = registry.open("derekt", "linkedin", "the draft")

    await handlers.on_approve(ack, click(request_id), slack)

    assert request.decision == "approved"
    assert request.user == "U123"
    assert request.event.is_set()

    update = slack.update
    assert update["channel"] == "C0DEREKT"
    assert update["ts"] == "1234.5678"
    assert "Approved by <@U123>" in header_of(update["blocks"])
    assert "actions" not in [block["type"] for block in update["blocks"]]


async def test_approving_an_expired_request_says_so_instead_of_failing(registry, slack):
    """The draft still has to stay on screen as a record of what was shown."""
    await handlers.on_approve(ack, click("long-gone"), slack)

    verdict = header_of(slack.update["blocks"])
    assert handlers.EXPIRED_SUFFIX in verdict
    assert slack.update["blocks"][1]["text"]["text"] == "the draft"


# --- edit --------------------------------------------------------------------


async def test_the_edit_button_opens_the_modal_without_waking_the_waiter(
    registry, slack
):
    """The verdict arrives with the submission; resolving here would skip the edit."""
    request_id, request = registry.open("derekt", "linkedin", "the draft")

    await handlers.on_edit(ack, click(request_id), slack)

    assert request.decision is None
    assert not request.event.is_set()
    assert not slack.updates

    view = slack.view
    assert view["callback_id"] == "edit_submit"
    assert view["blocks"][0]["element"]["initial_value"] == "the draft"
    assert view["blocks"][0]["label"]["text"] == "derekt · linkedin"
    assert json.loads(view["private_metadata"]) == {
        "request_id": request_id,
        "channel": "C0DEREKT",
        "ts": "1234.5678",
    }


async def test_editing_an_expired_request_shows_a_dead_end_modal(registry, slack):
    await handlers.on_edit(ack, click("long-gone"), slack)

    assert slack.view["title"]["text"] == "Expired"
    assert "submit" not in slack.view


async def test_a_draft_too_long_for_slacks_edit_box_is_refused_not_truncated(
    registry, slack
):
    """Truncating here would save a shortened post under the reviewer's name."""
    content = "x" * (blocks.MODAL_MAX_CHARS + 1)
    request_id, request = registry.open("derekt", "linkedin", content)

    await handlers.on_edit(ack, click(request_id), slack)

    assert slack.view["title"]["text"] == "Too long to edit"
    assert "3,001 characters" in slack.view["blocks"][0]["text"]["text"]
    assert request.decision is None  # still open for approve or reject


async def test_a_draft_exactly_at_the_limit_is_still_editable(registry, slack):
    request_id, _ = registry.open("derekt", "linkedin", "x" * blocks.MODAL_MAX_CHARS)

    await handlers.on_edit(ack, click(request_id), slack)

    assert slack.view["callback_id"] == "edit_submit"


async def test_the_edit_submission_approves_the_reviewers_text(registry, slack):
    request_id, request = registry.open("derekt", "linkedin", "the draft")
    body, view = submission(
        request_id, {"draft": {"content": {"value": "reviewer's version"}}}
    )

    await handlers.on_edit_submit(ack, body, view, slack)

    assert request.decision == "edited"
    assert request.content == "reviewer's version"
    assert request.user == "U123"
    assert request.event.is_set()

    update = slack.update
    assert "Edited and approved by <@U123>" in header_of(update["blocks"])
    # Rebuilt from scratch, and it must show the edited text, not the original.
    assert update["blocks"][1]["text"]["text"] == "reviewer's version"
    assert "`derekt` · `linkedin`" in update["blocks"][2]["elements"][0]["text"]


async def test_an_edit_submitted_after_the_request_expired_still_settles(
    registry, slack
):
    body, view = submission("long-gone", {"draft": {"content": {"value": "edited"}}})

    await handlers.on_edit_submit(ack, body, view, slack)

    blocks_out = slack.update["blocks"]
    assert handlers.EXPIRED_SUFFIX in header_of(blocks_out)
    assert blocks_out[1]["text"]["text"] == "edited"
    assert "`unknown` · `draft`" in blocks_out[2]["elements"][0]["text"]


# --- reject ------------------------------------------------------------------


async def test_the_reject_button_asks_why_without_waking_the_waiter(registry, slack):
    request_id, request = registry.open("derekt", "linkedin", "the draft")

    await handlers.on_reject(ack, click(request_id), slack)

    assert request.decision is None
    assert not request.event.is_set()
    assert slack.view["callback_id"] == "reject_submit"
    assert slack.view["blocks"][0]["optional"] is True


async def test_rejecting_an_expired_request_shows_a_dead_end_modal(registry, slack):
    await handlers.on_reject(ack, click("long-gone"), slack)

    assert slack.view["title"]["text"] == "Expired"
    assert "submit" not in slack.view


async def test_a_reason_is_recorded_and_quoted_back_into_the_channel(registry, slack):
    """A reason is what turns a rejection into a revision request."""
    request_id, request = registry.open("derekt", "linkedin", "the draft")
    body, view = submission(
        request_id, {"reason": {"value": {"value": "  hook is generic  "}}}
    )

    await handlers.on_reject_submit(ack, body, view, slack)

    assert request.decision == "rejected"
    assert request.reason == "hook is generic"  # stripped
    assert request.event.is_set()

    verdict = header_of(slack.update["blocks"])
    assert "Rejected by <@U123>" in verdict
    assert "> hook is generic" in verdict


@pytest.mark.parametrize("reason", ["", "   ", None])
async def test_a_blank_reason_rejects_without_asking_for_a_rewrite(
    registry, slack, reason
):
    request_id, request = registry.open("derekt", "linkedin", "the draft")
    body, view = submission(request_id, {"reason": {"value": {"value": reason}}})

    await handlers.on_reject_submit(ack, body, view, slack)

    assert request.decision == "rejected"
    assert request.reason is None
    assert "\n>" not in header_of(slack.update["blocks"])  # no quoted reason line


async def test_a_rejection_after_expiry_keeps_the_draft_it_can_still_see(
    registry, slack
):
    body, view = submission("long-gone", {"reason": {"value": {"value": "too late"}}})

    await handlers.on_reject_submit(ack, body, view, slack)

    blocks_out = slack.update["blocks"]
    assert handlers.EXPIRED_SUFFIX in header_of(blocks_out)
    assert "`unknown` · `draft`" in blocks_out[2]["elements"][0]["text"]
