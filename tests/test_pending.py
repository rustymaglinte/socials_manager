"""app.transports.slack_approval.pending -- the waiter/listener rendezvous.

Plain asyncio, no workspace needed. The behaviour that matters is the handoff:
a coroutine suspended on the Event has to wake with the verdict the handler
recorded, and a verdict for a request that is already gone has to be reported as
gone rather than silently dropped.
"""

import asyncio

import pytest

from app.transports.slack_approval.pending import PendingRegistry


@pytest.fixture
def pending() -> PendingRegistry:
    """A private registry -- the module-level one is process-wide state."""
    return PendingRegistry()


def test_open_registers_the_request_under_a_fresh_id(pending):
    request_id, request = pending.open("derekt", "linkedin", "draft body")

    assert pending.get(request_id) is request
    assert (request.brand, request.platform, request.content) == (
        "derekt",
        "linkedin",
        "draft body",
    )
    assert request.decision is None
    assert not request.event.is_set()


def test_ids_are_unique_per_request(pending):
    first, _ = pending.open("derekt", "linkedin", "a")
    second, _ = pending.open("derekt", "linkedin", "a")

    assert first != second


def test_get_of_an_unknown_id_is_none_not_an_error(pending):
    assert pending.get("nope") is None


def test_resolve_records_the_verdict_and_wakes_the_waiter(pending):
    request_id, request = pending.open("derekt", "x", "draft")

    assert pending.resolve(request_id, "approved", "U123") is True
    assert request.decision == "approved"
    assert request.user == "U123"
    assert request.event.is_set()


def test_resolve_replaces_the_content_on_an_edit(pending):
    request_id, request = pending.open("derekt", "x", "original")

    pending.resolve(request_id, "edited", "U123", content="reviewer's version")

    assert request.decision == "edited"
    assert request.content == "reviewer's version"


def test_resolve_keeps_the_content_when_no_edit_was_made(pending):
    request_id, request = pending.open("derekt", "x", "original")

    pending.resolve(request_id, "approved", "U123")

    assert request.content == "original"


def test_resolve_carries_the_rejection_reason(pending):
    request_id, request = pending.open("derekt", "x", "draft")

    pending.resolve(request_id, "rejected", "U123", reason="hook is generic")

    assert request.decision == "rejected"
    assert request.reason == "hook is generic"


def test_a_blank_reason_is_left_unset_so_the_run_can_stop(pending):
    request_id, request = pending.open("derekt", "x", "draft")

    pending.resolve(request_id, "rejected", "U123", reason=None)

    assert request.reason is None


def test_resolving_an_unknown_request_reports_it_as_gone(pending):
    """False is what the handler turns into '(request already expired)'."""
    assert pending.resolve("nope", "approved", "U123") is False


def test_a_second_click_after_the_waiter_gave_up_is_reported_as_gone(pending):
    request_id, _ = pending.open("derekt", "x", "draft")
    pending.close(request_id)

    assert pending.resolve(request_id, "approved", "U123") is False


def test_close_removes_the_request_and_is_idempotent(pending):
    request_id, request = pending.open("derekt", "x", "draft")

    assert pending.close(request_id) is request
    assert pending.get(request_id) is None
    assert pending.close(request_id) is None


async def test_a_suspended_waiter_wakes_with_the_reviewers_verdict(pending):
    request_id, request = pending.open("derekt", "linkedin", "original")

    async def waiter() -> tuple[str, str]:
        await request.event.wait()
        return request.decision, request.content

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # let the waiter reach the await

    pending.resolve(request_id, "edited", "U123", content="reviewer's version")

    assert await asyncio.wait_for(task, timeout=1) == ("edited", "reviewer's version")


async def test_a_waiter_that_times_out_leaves_no_entry_behind(pending):
    request_id, request = pending.open("derekt", "linkedin", "draft")

    with pytest.raises(TimeoutError):
        try:
            await asyncio.wait_for(request.event.wait(), timeout=0.01)
        finally:
            pending.close(request_id)

    assert pending.get(request_id) is None


async def test_two_brands_requests_stay_separate(pending):
    derekt_id, derekt = pending.open("derekt", "linkedin", "derekt draft")
    personal_id, personal = pending.open("personal", "linkedin", "personal draft")

    pending.resolve(derekt_id, "approved", "U123")

    assert derekt.event.is_set()
    assert not personal.event.is_set()
    assert pending.get(personal_id).content == "personal draft"
