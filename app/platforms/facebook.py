"""Publishing to a Facebook Page via the Graph API.

Pages only. Personal-profile posting went away with `publish_actions` in Graph
v3.0 and has not come back (SPECS 7.1), so there is no profile path here to
choose between.

Brand-blind by contract (import-linter, "Adapters are brand-blind"): this module
is handed a page id and a token, and never learns whose Page it is. Resolving a
brand to its credentials happens above, in the caller. That is what makes
cross-brand posting structurally impossible (SPECS D3) instead of a rule each
adapter has to remember.

On app review: SPECS 7.1 calls `pages_manage_posts` the longest lead time in the
project. That is true for posting to Pages *other people* own. An app left in
Development Mode has Standard Access to Pages its own developer administers,
which is every Page this product touches -- so no review gates this adapter.

Text and link posts only. Photos and video are separate endpoints (/photos,
/videos) with an upload step in front of them; they arrive with the media
pipeline (SPECS Q5).
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Pinned rather than floating. Meta supports each version for about two years and
# changes behaviour between them; an unversioned call silently follows whatever
# Meta currently defaults to, which moves underneath us.
API_VERSION = "v25.0"
GRAPH_URL = f"https://graph.facebook.com/{API_VERSION}"

# Generous: this is one request on a path a human is already waiting at the end
# of, and a slow publish is better than a false failure that tempts a retry.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Longer, because an image upload sends a megabyte or so over a connection to
# Manila before Graph starts processing it. Still bounded: a hung upload should
# surface as a failure a human can retry, not as a run that never returns.
UPLOAD_TIMEOUT_SECONDS = 90.0

# Graph error codes worth another attempt. Everything else -- an expired token
# (190), a missing permission (200), a message Meta rejected -- fails identically
# on the next attempt, so retrying only burns quota and delays the operator
# finding out. Consulted by the publisher's backoff/dead-letter split (FR-14).
_RETRYABLE_CODES = frozenset(
    {
        1,  # unknown transient error
        2,  # service temporarily unavailable
        4,  # app-level rate limit
        17,  # user-level rate limit
        32,  # page-level rate limit
        341,  # application limit reached
        613,  # calls-per-second limit
    }
)


class PublishFailed(RuntimeError):
    """A post did not go up.

    Carries `retryable` so the publisher can choose between backing off and
    dead-lettering (FR-14) without re-parsing Graph's error codes, and
    `trace_id` because fbtrace_id is the first thing Meta support asks for.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        trace_id: str | None = None,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.trace_id = trace_id
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class PublishedPost:
    """What a successful publish leaves behind."""

    # Graph's composite id, "{page_id}_{post_id}". Kept whole: it is what
    # /{post-id} endpoints take, so splitting it only creates a rejoin later.
    id: str
    page_id: str
    # False when the post was created unpublished. Recorded rather than inferred
    # so a caller can say "draft created" instead of claiming it went live.
    published: bool = True
    # Set only for image posts. /photos answers with a photo id and, when the
    # post went live, a separate composite post id -- two different handles for
    # two different objects, and deleting the wrong one leaves the other behind.
    photo_id: str | None = None

    @property
    def url(self) -> str:
        """Permalink, built rather than returned.

        /feed answers with an id and nothing else, and the operator confirming a
        post in Slack wants something clickable.

        Only resolves once the post is published -- an unpublished post has an
        id but no public page, so this 404s until `published` is true.
        """
        return f"https://www.facebook.com/{self.id}"


def _error_from(response: httpx.Response) -> PublishFailed:
    """Turn a Graph error response into something an operator can act on.

    Graph's own error body is the useful one -- it carries the code that decides
    retryability and the fbtrace_id support asks for -- but a 5xx raised by a
    proxy in front of Meta arrives as HTML, so parsing is best-effort and the
    status code is the fallback signal.
    """
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}

    code = error.get("code")
    detail = error.get("message") or response.text[:200].strip() or "no detail given"

    return PublishFailed(
        f"Facebook rejected the post (HTTP {response.status_code}): {detail}",
        code=code,
        subcode=error.get("error_subcode"),
        trace_id=error.get("fbtrace_id"),
        status=response.status_code,
        # No parseable code on a 5xx means Meta was unavailable rather than the
        # post being wrong, which is worth another attempt.
        retryable=code in _RETRYABLE_CODES
        or (code is None and response.status_code >= 500),
    )


async def _send(
    *,
    url: str,
    headers: dict[str, str],
    client: httpx.AsyncClient | None,
    timeout: float,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> dict:
    """One POST to Graph, with the client lifecycle and error shape handled once.

    Shared by the text and image paths because the parts worth getting right --
    closing a client we opened even when the call fails, turning a Graph error
    body into a classified PublishFailed -- are identical, and a second copy is
    a second place for them to drift.
    """
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await http.post(url, data=data, files=files, headers=headers)
    except httpx.RequestError as error:
        # Marked retryable, but honestly so: a connect failure certainly posted
        # nothing, while a read timeout may have landed and simply lost the
        # answer. Nothing here can tell those apart, which is the other half of
        # why dedupe belongs in the store rather than in the adapter (FR-13).
        raise PublishFailed(
            f"Could not reach the Graph API: {error}", retryable=True
        ) from error
    finally:
        if owned:
            await http.aclose()

    if response.is_error:
        raise _error_from(response)

    return response.json()


async def publish(
    *,
    page_id: str,
    access_token: str,
    message: str,
    link: str | None = None,
    published: bool = True,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PublishedPost:
    """Post to a Page's feed. Returns the published post, or raises PublishFailed.

    Keyword-only throughout: `page_id` and `access_token` are both opaque
    strings, and transposing them positionally would fail somewhere far from the
    mistake.

    `published=False` creates the post without putting it in the feed -- no
    follower sees it and no notification fires. It is how a change gets tested
    against the real Page and the real token rather than a stand-in, and it is
    also the first half of scheduling: `scheduled_publish_time` is only honoured
    on a post created unpublished.

    `client` is injectable so the publisher can reuse one connection pool across
    a batch, and so tests can hand in an httpx.MockTransport instead of reaching
    the network. Omitted, a client is opened and closed around the one call.

    Not idempotent, and it cannot be: Graph has no idempotency key for feed
    posts, so calling this twice publishes twice. Not double-posting under retry
    (FR-13) is the store's job, which is the reason the agent does not own the
    publishing state machine (SPECS D2).
    """
    # The failures most likely in practice are an unset env var and a page_id
    # left at its brand.yaml placeholder, and both are worth catching before a
    # round trip. The placeholder itself is the caller's check -- `configured` on
    # the Account knows what a TODO looks like, and this module stays brand-blind.
    if not access_token:
        raise PublishFailed("No Page access token supplied")
    if not page_id:
        raise PublishFailed("No page id supplied")
    if not message.strip():
        raise PublishFailed("Refusing to publish an empty message")

    payload = {"message": message}
    if link:
        payload["link"] = link
    if not published:
        # Sent as the literal string rather than a bool: what Graph reads is the
        # form-encoded value, and leaving that to the HTTP client's idea of how
        # to spell False is a silently-published post if it ever spells it "False".
        # Omitted entirely when publishing, since Graph already defaults to true.
        payload["published"] = "false"

    # The token rides in the header, not the query string or the form body: URLs
    # land in access logs, proxy logs and exception reprs, and a Page token that
    # never expires is the last secret that should be sitting in any of them.
    headers = {"Authorization": f"Bearer {access_token}"}

    body = await _send(
        url=f"{GRAPH_URL}/{page_id}/feed",
        headers=headers,
        data=payload,
        client=client,
        timeout=timeout,
    )

    post_id = body.get("id")
    if not post_id:
        # A 2xx carrying no id should not happen. Failing beats returning a
        # PublishedPost whose permalink would 404.
        raise PublishFailed(f"Graph API returned no post id: {body!r}")

    logger.info(
        "%s Facebook Page %s as %s",
        "Published to" if published else "Created an unpublished post on",
        page_id,
        post_id,
    )
    return PublishedPost(id=post_id, page_id=page_id, published=published)


async def publish_photo(
    *,
    page_id: str,
    access_token: str,
    image: bytes,
    message: str = "",
    published: bool = True,
    filename: str = "post.png",
    client: httpx.AsyncClient | None = None,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
) -> PublishedPost:
    """Post an image with a caption. Returns the published post, or raises.

    A different endpoint from `publish`, not a flag on it: /photos takes the
    image as multipart and calls the text `caption` rather than `message`, so
    folding the two together would mean a function whose arguments contradict
    each other depending on which one you used.

    An empty `message` is allowed -- an image can carry the whole post -- which
    is why the emptiness check here is on the image instead.

    Shares `publish`'s honesty about idempotency: uploading twice posts twice.
    """
    if not access_token:
        raise PublishFailed("No Page access token supplied")
    if not page_id:
        raise PublishFailed("No page id supplied")
    if not image:
        raise PublishFailed("Refusing to publish an empty image")

    data = {"caption": message}
    if not published:
        # Same literal-string reasoning as the text path: anything Graph does not
        # read as false publishes to a live Page.
        data["published"] = "false"

    body = await _send(
        url=f"{GRAPH_URL}/{page_id}/photos",
        headers={"Authorization": f"Bearer {access_token}"},
        data=data,
        files={"source": (filename, image, "image/png")},
        client=client,
        timeout=timeout,
    )

    # /photos answers with the photo's own id, plus `post_id` for the feed story
    # it created -- but only once published, since an unpublished photo has no
    # story yet. Prefer the story id where there is one: that is what permalinks
    # and /{post-id} operations take.
    photo_id = body.get("id")
    post_id = body.get("post_id") or photo_id
    if not post_id:
        raise PublishFailed(f"Graph API returned no photo id: {body!r}")

    logger.info(
        "%s Facebook Page %s as %s (photo %s)",
        "Published image to" if published else "Created an unpublished image post on",
        page_id,
        post_id,
        photo_id,
    )
    return PublishedPost(
        id=post_id, page_id=page_id, published=published, photo_id=photo_id
    )
