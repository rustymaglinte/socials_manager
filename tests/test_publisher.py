"""app.workers.publisher -- how one post's outcome is classified.

No database and no network. What is worth testing here is the decision the
publisher makes about each failure -- retry, or stop and tell someone -- and the
guarantee that a claimed row is *always* settled, whatever went wrong. A claimed
post left in PUBLISHING is invisible for fifteen minutes until the stale-claim
sweep finds it, so "every path settles" matters more than any single path.
"""

import uuid

import httpx
import pytest

from app.credentials import CredentialsMissing
from app.domain.brand import BrandNotFound
from app.platforms.facebook import PublishedPost, PublishFailed
from app.store.repositories import DuePost
from app.store.repositories.schedules import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    backoff_for,
)
from app.workers import publisher


@pytest.fixture
def settled(monkeypatch):
    """Capture what the publisher would have written, instead of writing it."""
    recorded: list[dict] = []

    async def fake_failure(post_id, message, *, retryable):
        recorded.append(
            {"outcome": "failed", "message": message, "retryable": retryable}
        )

    async def fake_published(session, post_id, *, external_id, url=None):
        recorded.append(
            {"outcome": "published", "external_id": external_id, "url": url}
        )

    class NullTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(publisher, "_settle_failure", fake_failure)
    monkeypatch.setattr(publisher, "mark_published", fake_published)
    monkeypatch.setattr(publisher, "transaction", lambda: NullTransaction())
    return recorded


def due(**overrides) -> DuePost:
    defaults = {
        "id": uuid.uuid4(),
        "brand_slug": "derekt",
        "platform": "facebook",
        "body": "A post that cleared review.",
        "attempts": 1,
    }
    return DuePost(**{**defaults, **overrides})


async def run(post, publish=None, settled_list=None, monkeypatch=None):
    """Drive publish_one with a stubbed platform call."""
    if publish is not None:
        monkeypatch.setitem(publisher._PUBLISHERS, "facebook", publish)
    async with httpx.AsyncClient() as client:
        return await publisher.publish_one(post, client)


# --- the failures that must not be retried ---------------------------------


async def test_a_platform_with_no_adapter_is_dead_lettered(settled):
    assert await run(due(platform="tiktok")) is False
    assert settled == [
        {"outcome": "failed", "message": settled[0]["message"], "retryable": False}
    ]
    assert "tiktok" in settled[0]["message"]


async def test_an_empty_post_with_nothing_to_show_is_refused(settled):
    """No retry will conjure the words, and an empty post is worse than none."""
    assert await run(due(body="   ")) is False
    assert settled[0]["retryable"] is False
    assert "neither text nor a graphic" in settled[0]["message"]


async def test_an_empty_caption_is_fine_when_a_graphic_carries_the_post(
    settled, monkeypatch
):
    """A card can be the whole post, which is why the adapter's /photos path
    checks the image for emptiness rather than the message."""
    sent = {}

    async def ok(post, client):
        sent["image"] = post.image
        sent["message"] = post.body
        return PublishedPost(id="67890_1", page_id="67890", published=True)

    assert await run(due(body="", image=b"PNG"), ok, monkeypatch=monkeypatch) is True
    assert sent == {"image": b"PNG", "message": ""}
    assert settled[0]["outcome"] == "published"


async def test_a_post_with_a_graphic_carries_the_approved_bytes_to_the_adapter(
    settled, monkeypatch
):
    """Carried through the queue, never re-rendered: what uploads has to be the
    image the reviewer was shown (C-1), and the worker has no renderer anyway."""
    seen = {}

    async def ok(post, client):
        seen["image"] = post.image
        return PublishedPost(id="67890_2", page_id="67890", published=True)

    assert await run(due(image=b"\x89PNG-approved"), ok, monkeypatch=monkeypatch) is True
    assert seen["image"] == b"\x89PNG-approved"


async def test_a_text_post_reaches_the_adapter_with_no_image(settled, monkeypatch):
    seen = {}

    async def ok(post, client):
        seen["image"] = post.image
        return PublishedPost(id="67890_3", page_id="67890", published=True)

    assert await run(due(), ok, monkeypatch=monkeypatch) is True
    assert seen["image"] is None


async def test_a_missing_token_is_not_weather(settled, monkeypatch):
    """An operator has to fix this; retrying only delays them finding out."""

    async def raise_missing(post, client):
        raise CredentialsMissing("Set FB_PAGE_TOKEN_DEREKT in .env")

    assert await run(due(), raise_missing, monkeypatch=monkeypatch) is False
    assert settled[0]["retryable"] is False
    assert "FB_PAGE_TOKEN_DEREKT" in settled[0]["message"]


async def test_a_brand_directory_that_vanished_is_not_retried(settled, monkeypatch):
    async def raise_missing(post, client):
        raise BrandNotFound("No brand.yaml at brands/derekt/brand.yaml")

    assert await run(due(), raise_missing, monkeypatch=monkeypatch) is False
    assert settled[0]["retryable"] is False


# --- the failures that should be retried -----------------------------------


async def test_the_adapters_own_classification_is_trusted(settled, monkeypatch):
    """The adapter reads the platform's error codes; the publisher does not
    second-guess which of them are worth another attempt."""

    async def rate_limited(post, client):
        raise PublishFailed("rate limited", code=32, retryable=True)

    assert await run(due(), rate_limited, monkeypatch=monkeypatch) is False
    assert settled[0]["retryable"] is True


async def test_a_refused_post_is_not_retried(settled, monkeypatch):
    async def refused(post, client):
        raise PublishFailed("token expired", code=190, retryable=False)

    assert await run(due(), refused, monkeypatch=monkeypatch) is False
    assert settled[0]["retryable"] is False


async def test_the_trace_id_survives_into_the_record(settled, monkeypatch):
    """fbtrace_id is the first thing Meta support asks for."""

    async def failed(post, client):
        raise PublishFailed("nope", trace_id="AbC123", retryable=True)

    await run(due(), failed, monkeypatch=monkeypatch)
    assert "AbC123" in settled[0]["message"]


async def test_an_unexpected_exception_still_settles_the_row(settled, monkeypatch):
    """The broad except in publish_one earns its keep here: a row left in
    PUBLISHING waits fifteen minutes for the stale sweep."""

    async def explode(post, client):
        raise ZeroDivisionError("something nobody predicted")

    assert await run(due(), explode, monkeypatch=monkeypatch) is False
    assert settled[0]["outcome"] == "failed"
    assert settled[0]["retryable"] is True
    assert "ZeroDivisionError" in settled[0]["message"]


# --- success ---------------------------------------------------------------


async def test_a_published_post_records_its_platform_id(settled, monkeypatch):
    async def ok(post, client):
        return PublishedPost(id="67890_111", page_id="67890", published=True)

    assert await run(due(), ok, monkeypatch=monkeypatch) is True
    assert settled[0]["outcome"] == "published"
    assert settled[0]["external_id"] == "67890_111"
    assert settled[0]["url"] == "https://www.facebook.com/67890_111"


async def test_an_unpublished_post_records_no_url(settled, monkeypatch):
    """A draft on the Page has an id but no public page; a permalink would 404."""

    async def draft(post, client):
        return PublishedPost(id="67890_111", page_id="67890", published=False)

    await run(due(), draft, monkeypatch=monkeypatch)
    assert settled[0]["url"] is None


# --- FR-15, the kill switch ------------------------------------------------


async def test_the_kill_switch_halts_publishing(monkeypatch, tmp_path):
    """"Without a deploy" (FR-15): a file, because an environment variable
    needs a restart and the moment you reach for this is the moment you do not
    want to restart anything."""
    switch = tmp_path / "PAUSE_PUBLISHING"
    switch.write_text("")
    monkeypatch.setattr(publisher, "KILL_SWITCH", switch)

    def refuse(*args, **kwargs):
        raise AssertionError("claimed work while halted")

    monkeypatch.setattr(publisher, "claim_due", refuse)
    async with httpx.AsyncClient() as client:
        assert await publisher.run_once(client) == 0


# --- FR-14, the backoff ----------------------------------------------------


def test_the_first_retry_waits_a_minute():
    assert backoff_for(1).total_seconds() == BASE_BACKOFF_SECONDS


def test_the_wait_doubles():
    waits = [backoff_for(n).total_seconds() for n in (1, 2, 3, 4)]
    assert waits == [60, 120, 240, 480]


def test_the_wait_is_capped():
    """Past an hour a human wants to know rather than wait longer."""
    assert backoff_for(50).total_seconds() == MAX_BACKOFF_SECONDS


def test_a_first_attempt_still_gets_a_real_wait():
    """attempts increments at claim time, so 0 should not be reachable -- but a
    negative exponent would be a zero-second retry storm if it ever were."""
    assert backoff_for(0).total_seconds() == BASE_BACKOFF_SECONDS
