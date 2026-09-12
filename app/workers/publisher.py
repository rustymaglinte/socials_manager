"""Claim approved posts and publish them. SPECS D2, FR-12 through FR-15.

    python -m app.workers.publisher

The half of the system that keeps working when the other half is down. It never
imports LangChain, LangGraph or `app.agent` -- enforced, not merely intended,
by the "Publisher never talks to the model" contract in pyproject.toml (C-4).
An agent crash loses a conversation; it does not lose an approved post.

The shape of one cycle, and the reason it is three steps rather than one:

    claim (transaction, committed)  ->  publish (no transaction)  ->  settle

Holding a transaction across the platform call would keep a row lock and a
pooled connection open for the length of an HTTP round trip to Meta -- up to 90
seconds on an upload -- while blocking every other worker from the row. So the
claim commits first, on its own.

What makes this safe to retry is the claim, not the adapter. Graph has no
idempotency key for feed posts, so `publish` called twice posts twice; the
guarantee lives in `state = PUBLISHING` being committed before the call and in
there being no lifecycle edge back to SCHEDULED without passing through FAILED.
"""

import argparse
import asyncio
import logging
import os
import socket
import sys
from datetime import timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.credentials import CredentialsMissing, credentials_for
from app.domain.brand import BrandNotFound, all_brands, load_brand
from app.platforms.facebook import (
    UPLOAD_TIMEOUT_SECONDS,
    PublishedPost,
    PublishFailed,
)
from app.platforms.facebook import publish as facebook_publish
from app.platforms.facebook import publish_photo as facebook_publish_photo
from app.store.engine import dispose, transaction
from app.store.repositories import (
    claim_due,
    mark_failed,
    mark_published,
    release_stale_claims,
    sync_brands,
)

logger = logging.getLogger(__name__)

# Explicitly, and this is the one process that has to say so. Every other entry
# point gets .env for free -- `app.main` and `app.scheduler` call this, and both
# would inherit it anyway through the Slack client and the LLM factory, which
# call it at import. This process imports neither, because C-4 forbids it from
# importing the model at all. The isolation contract is therefore exactly why
# the publisher alone was reading an environment .env had never been loaded into.
#
# The failure that hid it: `app.config` reads .env through pydantic's `env_file`,
# which fills the Settings model without touching os.environ. So the database
# connected, the worker looked healthy, and only `os.getenv` in
# `app.credentials` came back empty -- which surfaces as "Set FB_PAGE_TOKEN_<X>
# in .env" against a .env that has it, and dead-letters every post as a
# configuration error no operator can find.
load_dotenv()

# How often to look for work. Posts are scheduled to the minute at best, so
# polling faster buys nothing and costs a query.
POLL_SECONDS = 30

# Claimed per cycle. Small: each one is an HTTP call to a platform, and a big
# batch only means a longer stretch during which a restart strands claims.
BATCH_SIZE = 10

# A claim older than this belonged to a worker that is not coming back. Well
# clear of the adapter's 90-second upload timeout, so a slow publish is never
# mistaken for a dead one.
STALE_CLAIM_AFTER = timedelta(minutes=15)

# FR-15: a global kill switch that halts publishing without a deploy. A file
# rather than an environment variable, because a variable needs a restart to
# change and the moment you want this is the moment you do not want to restart
# anything. Create it to stop, delete it to resume:
#
#     New-Item PAUSE_PUBLISHING
#
# Checked every cycle, so it takes effect within POLL_SECONDS. Deliberately not
# a database row: the switch has to work when the reason you are reaching for it
# is that something is wrong with the database.
KILL_SWITCH = Path(os.getenv("PUBLISHING_KILL_SWITCH", "PAUSE_PUBLISHING"))

# The same switch, spelled for a host you cannot put a file on.
#
# The file is the right lever where there is a filesystem to reach: no restart,
# and it works when the reason you want it is that the database is unwell. On a
# container it is not a lever at all -- no shell to create it in, and an
# ephemeral disk that forgets it at the next redeploy. There the argument
# inverts exactly: setting a variable is the one-click operation.
#
# So both, and the deployment picks. Deliberately the same name as the file, so
# there is one thing to remember: create PAUSE_PUBLISHING, or set it to 1.
PAUSE_VAR = "PAUSE_PUBLISHING"

# Spelled out rather than "anything non-empty". `PAUSE_PUBLISHING=false` set by
# somebody who believes they are turning the switch *off* must not stop the
# queue -- that is the one misreading with a cost attached.
_YES = frozenset({"1", "true", "yes", "on"})


def publishing_paused() -> bool:
    """Whether FR-15's switch is on, by either spelling.

    Checked every cycle rather than at startup, so it takes effect within
    POLL_SECONDS -- the moment you want this is the moment you do not want to
    restart anything.
    """
    setting = (os.getenv(PAUSE_VAR) or "").strip().lower()
    if setting:
        return setting in _YES
    return KILL_SWITCH.exists()


def worker_name() -> str:
    """Who holds a claim. Recorded so a stale one can be traced to a process."""
    return f"{socket.gethostname()}:{os.getpid()}"


def graph_client() -> httpx.AsyncClient:
    """The client every publish runs on, however this process was started.

    One factory rather than a literal at each entry point, because `serve` and
    `--once` must not disagree about it: both build a client once and hand it to
    the adapter for every post in the batch.

    The timeout is set here as well as per request. The adapter already passes
    its own budget on each call, so this is the backstop rather than the
    mechanism -- but a bare `httpx.AsyncClient()` defaults to five seconds, and
    five seconds is not enough to push a megabyte of PNG to Meta. That is worth
    stating in the one place the pool is built, so the next caller to forget an
    argument inherits the upload budget instead of the default.
    """
    return httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SECONDS)


async def _publish_facebook(post, client: httpx.AsyncClient) -> PublishedPost:
    """Resolve this brand's Page and post to it, with its graphic if it has one.

    The brand is loaded here, at the last moment, rather than carried through
    the queue: brand.yaml is the source of truth for a Page id, and a post
    approved yesterday should go to the Page the brand names today.

    The *image*, by contrast, is carried through the queue rather than made
    here, and that asymmetry is deliberate. A Page id is configuration and
    should be current; a graphic is the artefact a human approved and must be
    the one they saw (C-1). Re-rendering it would also put Chromium in this
    process, which is a few hundred megabytes the worker has no other use for.

    Two endpoints, not a flag: /photos takes multipart and calls the text
    `caption`, so the adapter keeps them as separate functions and so does this.
    """
    brand = load_brand(post.brand_slug)
    page = credentials_for(brand, "facebook")

    if post.image:
        return await facebook_publish_photo(
            page_id=page.external_id,
            access_token=page.token,
            image=post.image,
            message=post.body,
            client=client,
        )

    return await facebook_publish(
        page_id=page.external_id,
        access_token=page.token,
        message=post.body,
        client=client,
    )


_PUBLISHERS = {"facebook": _publish_facebook}


async def _settle_failure(post_id, message: str, *, retryable: bool) -> None:
    async with transaction() as session:
        await mark_failed(session, post_id, error=message, retryable=retryable)


async def publish_one(post, client: httpx.AsyncClient) -> bool:
    """Publish one claimed post and record the outcome. True if it went up.

    Every failure path ends in a settled row. A claimed post that this function
    returns without settling would sit in PUBLISHING until the stale-claim
    sweep found it, which is a fifteen-minute delay on an error we already know
    about -- so the exception handling here is broad on purpose.
    """
    publisher = _PUBLISHERS.get(post.platform)
    if publisher is None:
        # Not retryable: an adapter that does not exist will not exist in a
        # minute either. Dead-letter it now so an operator sees it.
        await _settle_failure(
            post.id,
            f"No publisher for platform {post.platform!r}",
            retryable=False,
        )
        return False

    if not post.body.strip() and not post.image:
        # The approval carried neither words nor a graphic. Refusing beats
        # publishing an empty post, and no retry will conjure either.
        #
        # An empty caption *with* an image is allowed on purpose: a card can
        # carry the whole post, which is why the adapter's /photos path checks
        # the image for emptiness rather than the message.
        await _settle_failure(
            post.id,
            "The approved post has neither text nor a graphic; nothing to publish",
            retryable=False,
        )
        return False

    try:
        published = await publisher(post, client)
    except (CredentialsMissing, BrandNotFound) as error:
        # Configuration, not weather. Retrying cannot fix a missing token or a
        # brand directory that is not there; an operator has to.
        await _settle_failure(post.id, str(error), retryable=False)
        return False
    except PublishFailed as error:
        # The adapter has already classified this against the platform's own
        # error codes -- rate limits and outages are retryable, an expired token
        # or a refused message is not.
        detail = f" (fbtrace_id {error.trace_id})" if error.trace_id else ""
        await _settle_failure(
            post.id, f"{error}{detail}", retryable=error.retryable
        )
        return False
    except Exception as error:
        logger.exception("Unexpected failure publishing %s", post.id)
        await _settle_failure(
            post.id, f"{type(error).__name__}: {error}", retryable=True
        )
        return False

    async with transaction() as session:
        await mark_published(
            session,
            post.id,
            external_id=published.id,
            url=published.url if published.published else None,
        )
    logger.info("Published %s to %s: %s", post.id, post.platform, published.url)
    return True


async def run_once(client: httpx.AsyncClient, *, limit: int = BATCH_SIZE) -> int:
    """One cycle: sweep stale claims, claim what is due, publish it.

    Returns how many posts went up. Sequential rather than gathered: the batch
    is small, the platforms rate-limit per Page anyway (SPECS 7.2, 7.3), and
    publishing one at a time keeps a burst of failures from looking like a
    coordinated attack on someone's API quota.
    """
    if publishing_paused():
        logger.warning(
            "Publishing is halted (%s exists, or %s is set). Remove it to resume.",
            KILL_SWITCH,
            PAUSE_VAR,
        )
        return 0

    async with transaction() as session:
        await release_stale_claims(session, older_than=STALE_CLAIM_AFTER)

    # Committed before anything is published: until this transaction closes, no
    # other worker can see that these rows are taken.
    async with transaction() as session:
        due = await claim_due(session, worker=worker_name(), limit=limit)

    published = 0
    for post in due:
        published += await publish_one(post, client)
    return published


async def register_brands() -> None:
    """Make sure every brand directory has its row. Self-healing on boot.

    `brand_slug` is a foreign key, so a brand added to brands/ since the last
    run would otherwise fail every insert that named it. Cheap and idempotent
    (ON CONFLICT DO NOTHING), so both entry points do it rather than only the
    long-running one -- a `--once` run from a scheduler hits a fresh database
    just as often as a service start does.
    """
    async with transaction() as session:
        added = await sync_brands(session, [brand.slug for brand in all_brands()])
    if added:
        logger.info("Registered new brand(s): %s", ", ".join(added))


async def serve() -> None:
    """Poll until stopped. The long-running shape (README, Processes)."""
    logger.info("Publisher %s polling every %ds", worker_name(), POLL_SECONDS)
    # One client for the life of the worker: connections to Graph are reused
    # across cycles rather than renegotiating TLS for every post.
    async with graph_client() as client:
        while True:
            try:
                await run_once(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Anything that escaped run_once is a bug or a database that
                # went away. Either way the answer is to wait and try again; a
                # publisher that exits on the first bad cycle is a publisher
                # that is down all weekend.
                logger.exception("Publish cycle failed; continuing")
            await asyncio.sleep(POLL_SECONDS)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single cycle and exit, rather than polling",
    )
    parser.add_argument(
        "--limit", type=int, default=BATCH_SIZE, help="posts to claim per cycle"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    try:
        await register_brands()
        if args.once:
            async with graph_client() as client:
                published = await run_once(client, limit=args.limit)
            logger.info("Published %d post(s)", published)
        else:
            await serve()
    except asyncio.CancelledError:
        logger.info("Publisher stopping")
    finally:
        # Claims already committed survive; the stale-claim sweep picks up
        # anything this process was mid-publish on.
        await dispose()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
