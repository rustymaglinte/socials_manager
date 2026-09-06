"""app.platforms.facebook -- the Graph API adapter.

No network: every test drives a MockTransport standing in for Graph. What is
actually worth pinning down here is in three groups.

The request we send, because it is the half Meta sees and the half no test
downstream can catch: the pinned API version, the token in a header rather than
a URL, and a Filipino post surviving form encoding intact.

The verdict we return, because `retryable` is the input to a retry loop that does
not exist yet (FR-14). Getting it wrong means either a dead post nobody retries
or a bad token retried until it rate-limits us.

Who closes the client, because the publisher will pass a pooled one in and an
adapter that closes a caller's client breaks the second post, not the first.
"""

from urllib.parse import parse_qs

import httpx
import pytest

from app.platforms.facebook import (
    API_VERSION,
    PublishedPost,
    PublishFailed,
    publish,
    publish_photo,
)

PAGE_ID = "67890"
TOKEN = "EAAG-not-a-real-page-token"


class TrackingClient(httpx.AsyncClient):
    """An AsyncClient that remembers being closed, so ownership can be asserted."""

    closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class FakeGraph:
    """A stand-in Graph API: records what it was sent, replies with what it was told to."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: httpx.Response | Exception = httpx.Response(
            200, json={"id": f"{PAGE_ID}_111"}
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def client(self) -> TrackingClient:
        return TrackingClient(transport=httpx.MockTransport(self._handle))

    @property
    def sent(self) -> httpx.Request:
        """The single request that was made, asserting there was exactly one."""
        assert len(self.requests) == 1, f"expected 1 request, got {len(self.requests)}"
        return self.requests[0]

    @property
    def form(self) -> dict[str, list[str]]:
        return parse_qs(self.sent.content.decode())


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


def post(graph: FakeGraph, **overrides):
    """publish() with the boilerplate filled in and the fake wired up."""
    kwargs = {
        "page_id": PAGE_ID,
        "access_token": TOKEN,
        "message": "Tara, kantahan tayo!",
        "client": graph.client(),
    }
    return publish(**{**kwargs, **overrides})


# --- what we send -----------------------------------------------------------


async def test_posts_to_the_pages_feed_on_the_pinned_api_version(graph):
    """The version is spelled out rather than read from the module under test.

    Interpolating API_VERSION here would make this assertion move with the code
    and quietly assert nothing. Meta supports a version for about two years, so
    bumping it is a decision with a deprecation date attached -- failing this
    test is how that decision gets made on purpose instead of in passing.
    """
    await post(graph)

    assert str(graph.sent.url) == f"https://graph.facebook.com/v25.0/{PAGE_ID}/feed"
    assert graph.sent.method == "POST"
    assert API_VERSION == "v25.0"


async def test_the_token_travels_in_the_header_and_nowhere_else(graph):
    """The regression this guards: `access_token` is also accepted as a query
    parameter, and moving it there would quietly put a never-expiring Page token
    into every access log between here and Meta."""
    await post(graph)

    assert graph.sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(graph.sent.url)
    assert TOKEN not in graph.sent.content.decode()


async def test_sends_the_message_and_omits_link_when_there_is_none(graph):
    await post(graph, message="Bagong videoke night ngayong Sabado.")

    assert graph.form == {"message": ["Bagong videoke night ngayong Sabado."]}


async def test_sends_link_alongside_the_message_when_given(graph):
    await post(graph, message="Panoorin!", link="https://youtu.be/abc123")

    assert graph.form["message"] == ["Panoorin!"]
    assert graph.form["link"] == ["https://youtu.be/abc123"]


async def test_publishing_says_nothing_about_published_at_all(graph):
    """Graph defaults to published. Sending the key anyway would mean trusting
    the client's spelling of True on the path that goes live."""
    await post(graph)

    assert "published" not in graph.form


async def test_an_unpublished_post_sends_the_literal_string_false(graph):
    """The failure this guards is the expensive one: anything Graph does not read
    as false publishes to a live Page, and a bool spelled "False" is not false."""
    await post(graph, published=False)

    assert graph.form["published"] == ["false"]


async def test_an_unpublished_post_does_not_claim_to_be_live(graph):
    """The caller reports this to a human. Saying "posted" about a draft is the
    kind of wrong that gets discovered by someone refreshing the Page."""
    published = await post(graph, published=False)

    assert published.published is False


async def test_filipino_text_and_emoji_survive_form_encoding(graph):
    """PinoySing posts in Filipino with hashtags and the odd emoji. A mangled
    encoding would not raise -- it would publish, wrongly, to a live Page."""
    body = "Kumusta! 🎤 Sino ang paborito mong kumanta? #PinoySing #videoke"

    await post(graph, message=body)

    assert graph.form["message"] == [body]


# --- what we return ---------------------------------------------------------


async def test_a_published_post_carries_its_id_and_a_usable_permalink(graph):
    graph.reply = httpx.Response(200, json={"id": f"{PAGE_ID}_5678"})

    published = await post(graph)

    assert published == PublishedPost(id=f"{PAGE_ID}_5678", page_id=PAGE_ID)
    assert published.url == f"https://www.facebook.com/{PAGE_ID}_5678"


async def test_a_success_with_no_id_is_a_failure_not_a_post(graph):
    """Returning here would hand the reviewer a permalink that 404s."""
    graph.reply = httpx.Response(200, json={})

    with pytest.raises(PublishFailed, match="no post id"):
        await post(graph)


# --- how failures are classified --------------------------------------------


async def test_an_expired_token_is_not_retryable_and_keeps_its_trace_id(graph):
    """Code 190 fails the same way forever; retrying it just delays the operator
    finding out. fbtrace_id is the first thing Meta support asks for."""
    graph.reply = httpx.Response(
        400,
        json={
            "error": {
                "message": "Error validating access token: Session has expired.",
                "code": 190,
                "error_subcode": 463,
                "fbtrace_id": "AbC123xyz",
            }
        },
    )

    with pytest.raises(PublishFailed) as caught:
        await post(graph)

    error = caught.value
    assert error.retryable is False
    assert (error.code, error.subcode, error.status) == (190, 463, 400)
    assert error.trace_id == "AbC123xyz"
    assert "Session has expired" in str(error)


async def test_a_rate_limit_is_retryable(graph):
    """Code 32 is the Page-level limit -- the same post will go up later."""
    graph.reply = httpx.Response(
        400, json={"error": {"message": "Page request limit reached", "code": 32}}
    )

    with pytest.raises(PublishFailed) as caught:
        await post(graph)

    assert caught.value.retryable is True


async def test_a_missing_permission_is_not_retryable(graph):
    """Code 200 needs a human to change something; a retry loop cannot fix it."""
    graph.reply = httpx.Response(
        403,
        json={"error": {"message": "Requires pages_manage_posts", "code": 200}},
    )

    with pytest.raises(PublishFailed) as caught:
        await post(graph)

    assert caught.value.retryable is False


async def test_an_unparseable_5xx_is_retryable(graph):
    """A gateway in front of Meta answers in HTML, so there is no code to read.
    A 5xx means Meta was unavailable, not that the post was wrong."""
    graph.reply = httpx.Response(502, text="<html><body>Bad Gateway</body></html>")

    with pytest.raises(PublishFailed) as caught:
        await post(graph)

    error = caught.value
    assert error.retryable is True
    assert error.code is None
    assert error.status == 502


async def test_an_unparseable_4xx_is_not_retryable(graph):
    """The asymmetry with the 5xx above is the point: without a code to read, the
    status is all that separates 'Meta is down' from 'our request is wrong'."""
    graph.reply = httpx.Response(400, text="not json")

    with pytest.raises(PublishFailed) as caught:
        await post(graph)

    assert caught.value.retryable is False


async def test_never_reaching_the_api_is_retryable(graph):
    graph.reply = httpx.ConnectError("connection refused")

    with pytest.raises(PublishFailed, match="Could not reach the Graph API") as caught:
        await post(graph)

    assert caught.value.retryable is True


# --- guards, none of which should cost a request ----------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"access_token": ""}, "No Page access token supplied"),
        ({"page_id": ""}, "No page id supplied"),
        ({"message": "   \n "}, "Refusing to publish an empty message"),
    ],
    ids=["no token", "no page id", "blank message"],
)
async def test_bad_input_fails_before_a_request_is_made(graph, overrides, expected):
    """An unset env var is the likeliest failure in practice. Catching it here
    keeps it off Meta's rate limit and puts the real cause in the message."""
    with pytest.raises(PublishFailed, match=expected):
        await post(graph, **overrides)

    assert graph.requests == []


# --- client ownership -------------------------------------------------------


async def test_a_caller_supplied_client_is_left_open(graph):
    """The publisher will reuse one pool across a batch. Closing it here would
    break every post after the first."""
    client = graph.client()

    await post(graph, client=client)

    assert client.closed is False
    await client.aclose()


async def test_a_client_we_opened_is_closed_even_when_the_post_fails(monkeypatch, graph):
    """Otherwise a run of failures leaks a connection pool per attempt."""
    opened = graph.client()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: opened)
    graph.reply = httpx.Response(400, json={"error": {"message": "nope", "code": 100}})

    with pytest.raises(PublishFailed):
        await publish(page_id=PAGE_ID, access_token=TOKEN, message="hi")

    assert opened.closed is True


# --- image posts --------------------------------------------------------------
#
# A different endpoint with a different parameter name for the same idea, so the
# tests below are mostly about not confusing the two paths. The expensive
# confusion is `published`: getting it wrong on an image post publishes a
# graphic to a live Page.

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def photo(graph: FakeGraph, **overrides):
    kwargs = {
        "page_id": PAGE_ID,
        "access_token": TOKEN,
        "image": PNG,
        "message": "Tara, kantahan tayo!",
        "client": graph.client(),
    }
    return publish_photo(**{**kwargs, **overrides})


async def test_an_image_goes_to_the_photos_edge_not_the_feed(graph):
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_5678"})

    await photo(graph)

    assert str(graph.sent.url) == f"https://graph.facebook.com/v25.0/{PAGE_ID}/photos"


async def test_the_image_is_uploaded_as_multipart(graph):
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_5678"})

    await photo(graph)

    assert graph.sent.headers["content-type"].startswith("multipart/form-data")
    body = graph.sent.content
    assert PNG in body, "the image bytes must survive to the wire"
    assert b'name="source"' in body


async def test_the_text_is_sent_as_caption_not_message(graph):
    """/photos names it `caption`. Sending `message` there is accepted and
    silently ignored, which publishes an image with no words under it."""
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_5678"})

    await photo(graph, message="Anong kanta mo? 🎤")

    body = graph.sent.content
    assert b'name="caption"' in body
    assert b'name="message"' not in body


async def test_an_image_post_prefers_the_story_id_over_the_photo_id(graph):
    """/photos returns both. The composite is the one permalinks and
    /{post-id} operations take; the photo id is a different object."""
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_5678"})

    post = await photo(graph)

    assert post.id == f"{PAGE_ID}_5678"
    assert post.photo_id == "999"
    assert post.url == f"https://www.facebook.com/{PAGE_ID}_5678"


async def test_an_unpublished_image_falls_back_to_the_photo_id(graph):
    """An unpublished photo has no feed story yet, so Graph returns no post_id.
    Without the fallback there would be no handle to delete it by."""
    graph.reply = httpx.Response(200, json={"id": "999"})

    post = await photo(graph, published=False)

    assert post.id == "999"
    assert post.photo_id == "999"
    assert post.published is False


async def test_an_unpublished_image_sends_the_literal_string_false(graph):
    graph.reply = httpx.Response(200, json={"id": "999"})

    await photo(graph, published=False)

    assert b'name="published"' in graph.sent.content
    assert b"false" in graph.sent.content


async def test_publishing_an_image_says_nothing_about_published(graph):
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_1"})

    await photo(graph)

    assert b'name="published"' not in graph.sent.content


async def test_an_image_post_may_carry_no_caption(graph):
    """A graphic can be the whole post -- so emptiness is checked on the image,
    not on the text, which is the opposite of the text path."""
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_1"})

    post = await photo(graph, message="")

    assert post.id == f"{PAGE_ID}_1"


async def test_an_empty_image_fails_before_a_request_is_made(graph):
    with pytest.raises(PublishFailed, match="empty image"):
        await photo(graph, image=b"")

    assert graph.requests == []


async def test_an_image_post_keeps_the_token_in_the_header(graph):
    graph.reply = httpx.Response(200, json={"id": "999", "post_id": f"{PAGE_ID}_1"})

    await photo(graph)

    assert graph.sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN.encode() not in graph.sent.content
