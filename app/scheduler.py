"""Start a brief on a timer, so a post is drafted the moment it is wanted.

    uv run python -m app.scheduler                   # serve: fire at each slot
    uv run python -m app.scheduler --once pinoysing  # one brief now, then exit

Run from the repo root: `[tool.uv] package = false`, so `app` is imported from
the working directory rather than from an installed distribution. `uv run` is
what makes that work without an activated venv, which is the difference between
a command that runs by hand and one Task Scheduler can run at 09:00.

Freshness is the whole design constraint, and it is why this is a timer rather
than a batch. Every firing searches the web at the moment it fires, and an
approved post publishes on the publisher's next poll, so nothing a reviewer sees
was researched hours before it goes out. The cost is the other side of that
trade and it is real: the reviewer has to be reachable during the slot, because
a draft nobody rules on inside its approval window is dropped rather than held.

Where this sits. It is a composition root beside `app.main`, deliberately not a
worker: the layers contract in pyproject.toml makes `app.agent` and `app.workers`
independent siblings, so nothing under `app/workers/` may invoke the model.
`app.workers.publisher` is the process this one feeds, and they stay separate
for the reason D2 gives -- a scheduler that falls over must not keep an already
approved post from going out.

Two shapes, mirroring the publisher's, and the same argument for each. `--once`
is for an external clock (Windows Task Scheduler, cron, systemd); the default
holds the Slack socket open and keeps its own. Prefer the default. A run blocks
on a human, so an externally-timed process has to stay alive through the
approval anyway, and starting a fresh one every two hours pays for a Slack
handshake, a connection pool and a Chromium launch every time.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app.domain.brand import BrandContext, BrandNotFound, all_brands, load_brand
from app.store.engine import dispose, transaction
from app.store.repositories import drafts_since
from app.transports.slack_approval.client import (
    DEFAULT_TIMEOUT_SECONDS,
    start_listener,
    stop_listener,
)
from app.transports.slack_approval.handlers import brief_run

logger = logging.getLogger(__name__)

load_dotenv()

# How late a slot may be drafted and still count as that slot's post. Past this
# the slot is skipped rather than caught up, which is the freshness rule applied
# to the scheduler's own downtime: a 09:00 brief written at 11:30 is not the
# 09:00 post arriving late, it is a stale post competing with the 11:00 one.
CATCH_UP_GRACE = timedelta(minutes=45)

# Left between an approval window closing and the next slot opening, so a
# reviewer is never looking at two live drafts for one brand at once.
APPROVAL_MARGIN = timedelta(minutes=30)

# Longest the loop sleeps before re-checking, however far off the next slot is.
# Bounded because a laptop that suspends for three hours wakes with a timer that
# has not fired: re-evaluating on a short leash turns that into a slot that is
# merely late, which CATCH_UP_GRACE can still rescue.
MAX_SLEEP_SECONDS = 900

# FR-15's switch, for drafting rather than publishing. A file for the same
# reason `PAUSE_PUBLISHING` is one: the moment you want it is the moment you do
# not want to restart anything. Create it to stop, delete it to resume:
#
#     New-Item PAUSE_DRAFTING
#
# Deliberately separate from the publisher's. Pausing new drafts while approved
# posts keep going out is the common case -- the model is saying something odd
# and the queue behind it is fine.
KILL_SWITCH = Path(os.getenv("DRAFTING_KILL_SWITCH", "PAUSE_DRAFTING"))

# The same switch, spelled for a host with no filesystem to put a file on --
# see `app.workers.publisher.PAUSE_VAR`, which carries the argument. Separate
# from the publisher's on purpose: pausing new drafts while approved posts keep
# going out is the common case.
PAUSE_VAR = "PAUSE_DRAFTING"

_YES = frozenset({"1", "true", "yes", "on"})


def drafting_paused() -> bool:
    """Whether FR-15's drafting switch is on, by either spelling."""
    setting = (os.getenv(PAUSE_VAR) or "").strip().lower()
    if setting:
        return setting in _YES
    return KILL_SWITCH.exists()

# Brands whose run has not finished yet. A run is spawned as a task so one
# brand's reviewer cannot hold up another brand's slot, which means the loop can
# come back around while the first is still waiting at the gate; without this a
# slow approval would put two drafts for one brand in front of one person.
_running: set[str] = set()

# Strong references to the spawned runs. asyncio keeps only a weak one, so a
# task that is not held here can be collected mid-await.
_tasks: set[asyncio.Task] = set()


def cron_brands(brands: tuple[BrandContext, ...]) -> list[BrandContext]:
    """The brands that asked to be on a timer, in slug order.

    A brand with no `cadence.every_hours`/`first_slot` produces no slots and is
    skipped, which is what keeps this opt-in: adding the two lines to one
    brand.yaml cannot start drafting for the brands that never asked.
    """
    return [brand for brand in sorted(brands, key=lambda b: b.slug) if brand.post_slots]


def slots_on(brand: BrandContext, day: datetime) -> list[datetime]:
    """This brand's slots on `day`'s date, as aware datetimes in its own zone.

    Built with `combine` rather than by replacing fields on an existing
    datetime: replacing the hour keeps whatever UTC offset the original had,
    which is the standard way to land an hour off in a zone that observes DST.
    """
    zone = ZoneInfo(brand.timezone)
    local = day.astimezone(zone)
    return [
        datetime.combine(local.date(), slot, tzinfo=zone) for slot in brand.post_slots
    ]


def next_slot(brand: BrandContext, now: datetime) -> datetime | None:
    """When this brand is next due, or None if it is not on a timer."""
    if not brand.post_slots:
        return None

    zone = ZoneInfo(brand.timezone)
    local = now.astimezone(zone)
    for moment in slots_on(brand, now):
        if moment > local:
            return moment

    # Past the last slot of the day: the next one is tomorrow's first.
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, brand.post_slots[0], tzinfo=zone)


def current_slot(brand: BrandContext, now: datetime) -> datetime | None:
    """The slot this moment belongs to, if drafting it now would still be fresh.

    The most recent slot that has passed, and only while it is inside
    CATCH_UP_GRACE. Returning the *latest* rather than the earliest unfilled one
    is what stops a restart from working through a backlog: a scheduler that
    comes up at 15:10 having missed 09:00, 11:00 and 13:00 owes the feed one
    fresh 15:00 post, not three stale ones in a row.
    """
    elapsed = [moment for moment in slots_on(brand, now) if moment <= now]
    if not elapsed:
        return None

    latest = elapsed[-1]
    if now - latest > CATCH_UP_GRACE:
        logger.debug(
            "%s: slot %s missed by %s; waiting for the next one",
            brand.slug,
            latest.strftime("%H:%M"),
            now - latest,
        )
        return None
    return latest


async def is_due(brand: BrandContext, now: datetime) -> bool:
    """Whether this brand's current slot still needs its draft.

    Answered from the database rather than from a flag in this process, so it
    survives a restart: "has 11:00 been drafted" is a row that exists or does
    not, and a scheduler that comes back up at 11:05 asks the same question the
    one that died at 11:01 was asking.
    """
    slot = current_slot(brand, now)
    if slot is None:
        return False

    async with transaction() as session:
        already = await drafts_since(session, brand_slug=brand.slug, since=slot)

    if already:
        logger.debug(
            "%s: slot %s already drafted (%d)", brand.slug, slot.strftime("%H:%M"), already
        )
        return False
    return True


def approval_window(brand: BrandContext, now: datetime) -> int:
    """Seconds the reviewer gets, in whole seconds for `request_approval`.

    Most of the gap to the next slot, less a margin. The default ten minutes is
    a floor rather than the starting point, because ten minutes is a number
    chosen for a run somebody started by hand and is watching -- for a draft
    that appeared while they were out, it is the difference between a post and
    a timeout.

    Capped at one slot's worth regardless, which matters only for the last slot
    of the day: the gap from 17:00 to tomorrow's 09:00 is sixteen hours, and
    without the cap the evening draft would be the one post that could be
    approved at breakfast and publish a day stale. Better to let it time out --
    an expired draft costs a search, a stale post costs the feed.
    """
    upcoming = next_slot(brand, now)
    if upcoming is None:
        return DEFAULT_TIMEOUT_SECONDS

    gap = min(upcoming - now, timedelta(hours=brand.every_hours)) - APPROVAL_MARGIN
    return max(int(gap.total_seconds()), DEFAULT_TIMEOUT_SECONDS)


async def fire(brand: BrandContext, now: datetime | None = None) -> None:
    """Draft one post for one brand, and wait out its approval.

    Never raises: `brief_run` reports a failure to the brand's own Slack channel
    and swallows it, which is the behaviour an unattended run needs -- there is
    nobody reading the log at 09:00, and the loop must not die on one bad brief.
    """
    now = now or datetime.now(UTC)
    timeout = approval_window(brand, now)

    _running.add(brand.slug)
    logger.info(
        "Briefing %s (reviewer has %d minute(s))", brand.slug, round(timeout / 60)
    )
    try:
        await brief_run(brand, approval_timeout=timeout)
    finally:
        _running.discard(brand.slug)


async def tick(brands: list[BrandContext], now: datetime | None = None) -> int:
    """Fire every brand that is due. Returns how many runs were started.

    Spawned rather than awaited: a run blocks on a human for most of an hour,
    and awaiting it here would mean one brand's slow reviewer silently costing
    another brand its slot.
    """
    if drafting_paused():
        logger.warning(
            "Drafting is paused (%s exists, or %s is set). Remove it to resume.",
            KILL_SWITCH,
            PAUSE_VAR,
        )
        return 0

    now = now or datetime.now(UTC)
    started = 0
    for brand in brands:
        if brand.slug in _running:
            logger.warning(
                "%s is still waiting on its last approval; skipping this slot",
                brand.slug,
            )
            continue
        if not await is_due(brand, now):
            continue

        task = asyncio.create_task(fire(brand, now))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        started += 1
    return started


def sleep_seconds(brands: list[BrandContext], now: datetime) -> float:
    """How long until something might need doing, capped at MAX_SLEEP_SECONDS."""
    upcoming = [moment for moment in (next_slot(b, now) for b in brands) if moment]
    if not upcoming:
        return MAX_SLEEP_SECONDS

    gap = (min(upcoming) - now).total_seconds()
    return max(min(gap, MAX_SLEEP_SECONDS), 1.0)


async def serve(brands: list[BrandContext]) -> None:
    """Fire each brand's slots until stopped. The long-running shape.

    Checks on the way in rather than sleeping first, so a scheduler started at
    11:05 picks up the 11:00 slot instead of idling until 13:00.
    """
    # Connected here rather than left to the first run that needs it. `run`
    # opens the socket on its way to the gate, so without this the process is
    # deaf until its first slot fires -- and a mention is the escape hatch for
    # exactly the hours the schedule does not cover, so "you can always mention
    # the bot" has to be true from boot rather than from 09:00.
    await start_listener()
    logger.info("Listening for mentions; drafting on schedule.")

    for brand in brands:
        logger.info(
            "%s: %s %s",
            brand.slug,
            ", ".join(slot.strftime("%H:%M") for slot in brand.post_slots),
            brand.timezone,
        )

    while True:
        now = datetime.now(UTC)
        try:
            await tick(brands, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A database that went away, most likely. The next slot is worth
            # trying either way; a scheduler that exits on one bad cycle is a
            # scheduler that is down all weekend.
            logger.exception("Scheduler cycle failed; continuing")

        delay = sleep_seconds(brands, datetime.now(UTC))
        logger.debug("Sleeping %.0fs", delay)
        await asyncio.sleep(delay)


def _select(slug: str | None) -> list[BrandContext]:
    """The brands this invocation is for, or exit saying why there are none."""
    if slug:
        try:
            return [load_brand(slug)]
        except BrandNotFound as error:
            raise SystemExit(str(error)) from error

    brands = cron_brands(all_brands())
    if not brands:
        raise SystemExit(
            "No brand is on a timer. Add `every_hours` and `first_slot` under "
            "`cadence` in brands/<slug>/brand.yaml, or name a brand to run once."
        )
    return brands


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "brand",
        nargs="?",
        help="only this brand (default: every brand with slots configured)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "draft now and exit, whatever the clock says -- for an external "
            "timer that owns the schedule"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    brands = _select(args.brand)
    try:
        if args.once:
            if drafting_paused():
                logger.warning(
                    "Drafting is paused (%s exists, or %s is set).",
                    KILL_SWITCH,
                    PAUSE_VAR,
                )
                return 0
            # Sequential: each run puts a draft in front of the same reviewer,
            # and asking about three at once is worse than asking three times.
            for brand in brands:
                await fire(brand)
        else:
            await serve(brands)
    except asyncio.CancelledError:
        logger.info("Scheduler stopping")
    finally:
        await stop_listener()
        await dispose()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
