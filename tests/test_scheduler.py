"""app.scheduler -- the timer that decides when a brand gets briefed.

This module had no test at all, which is uncomfortable for the process that
actually runs unattended: every other entry point is started by somebody who is
watching, and this one fires at 09:00 whether or not anyone is awake.

Three properties carry most of the risk and none of them is obvious from
reading the code:

- **Slots are built with `combine`, not by replacing an hour.** Replacing keeps
  whatever UTC offset the original datetime had, which is the ordinary way to
  land an hour out in a zone that observes DST.
- **`current_slot` returns the *latest* elapsed slot, not the earliest.** That
  is what stops a scheduler restarted at 15:10 from working through 09:00,
  11:00 and 13:00 as a backlog and posting three stale things in a row.
- **The approval window is capped at one slot's worth.** Without the cap the
  evening draft would be the one post approvable at breakfast, publishing a day
  stale.

No database and no Slack: `is_due`'s query and `fire`'s run are both stubbed,
because what is being tested is the arithmetic that decides whether to call
them.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import scheduler
from tests.conftest import make_brand

MANILA = "Asia/Manila"


def on_a_timer(**overrides):
    """A brand with slots: 09:00, 11:00, 13:00, 15:00, 17:00 Asia/Manila."""
    defaults = {
        "slug": "pinoysing",
        "timezone": MANILA,
        "every_hours": 2,
        "first_slot": time(9, 0),
        "max_per_day": 5,
    }
    return make_brand(**{**defaults, **overrides})


def manila(year, month, day, hour, minute=0) -> datetime:
    """A moment written in the brand's own clock, as the aware UTC the loop passes."""
    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(MANILA))
    return local.astimezone(UTC)


@pytest.fixture(autouse=True)
def _no_leftover_runs():
    """`_running` is a module global; a leaked slug would skip a later test's brand."""
    scheduler._running.clear()
    yield
    scheduler._running.clear()


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch, tmp_path):
    """Point the switch at a path that does not exist.

    A developer who left a PAUSE_DRAFTING in the repo root while debugging
    would otherwise turn every test in this file green for the wrong reason --
    `tick` returns 0 before it evaluates anything.
    """
    monkeypatch.setattr(scheduler, "KILL_SWITCH", tmp_path / "absent")


# --- which brands are on a timer at all ---------------------------------------


def test_a_brand_without_a_cadence_is_not_on_the_timer():
    """Opt-in per brand: adding the two keys to one brand.yaml must not start
    drafting for the brands that never asked."""
    by_hand = make_brand(slug="personal", every_hours=0, first_slot=None)

    assert by_hand.post_slots == ()
    assert scheduler.cron_brands((by_hand,)) == []


def test_brands_on_a_timer_come_back_in_slug_order():
    first = on_a_timer(slug="aaa")
    second = on_a_timer(slug="zzz")

    assert [b.slug for b in scheduler.cron_brands((second, first))] == ["aaa", "zzz"]


# --- the clock ----------------------------------------------------------------


def test_slots_are_the_brands_local_times_not_the_hosts():
    """A Manila brand posts at 09:00 Manila from a UTC box -- 01:00 UTC."""
    slots = scheduler.slots_on(on_a_timer(), manila(2026, 9, 11, 12))

    assert [s.strftime("%H:%M") for s in slots] == [
        "09:00", "11:00", "13:00", "15:00", "17:00"
    ]
    assert slots[0].astimezone(UTC).strftime("%H:%M") == "01:00"


def test_tomorrows_first_slot_is_built_in_tomorrows_offset():
    """The one place a slot is built for a date other than the one in hand.

    `slots_on` cannot get this wrong: it builds slots for the same date it was
    given, so the offset it starts from is already the right one, and zoneinfo
    resolves the rest from the wall time. `next_slot`'s tomorrow branch is
    different -- it crosses a date boundary, so an offset carried over from
    today is an hour of error on the far side of a transition.

    Asserted in a zone that observes DST because Asia/Manila does not. The
    module is general, and the failure would be a brand posting at 08:00 or
    10:00 for six months without anyone connecting it to the clocks changing.
    """
    brand = on_a_timer(timezone="America/New_York", max_per_day=1)
    zone = ZoneInfo("America/New_York")

    # The evening before the US spring-forward: EST now, EDT at tomorrow's slot.
    evening = datetime(2026, 3, 7, 20, tzinfo=zone).astimezone(UTC)
    upcoming = scheduler.next_slot(brand, evening)

    assert upcoming.strftime("%H:%M") == "09:00"
    assert upcoming.date() == datetime(2026, 3, 8).date()
    # EDT, not the EST the evening was in. The instant is what the timer sleeps
    # until, so an hour here is an hour of drift in when the post goes out.
    assert upcoming.utcoffset() == timedelta(hours=-4)
    assert upcoming.astimezone(UTC).strftime("%H:%M") == "13:00"


def test_the_next_slot_is_the_next_one_today():
    upcoming = scheduler.next_slot(on_a_timer(), manila(2026, 9, 11, 11, 30))
    assert upcoming.strftime("%H:%M") == "13:00"


def test_past_the_last_slot_the_next_one_is_tomorrows_first():
    upcoming = scheduler.next_slot(on_a_timer(), manila(2026, 9, 11, 20))

    assert upcoming.strftime("%H:%M") == "09:00"
    assert upcoming.date() == datetime(2026, 9, 12).date()


def test_a_brand_not_on_a_timer_has_no_next_slot():
    assert scheduler.next_slot(make_brand(every_hours=0, first_slot=None), manila(2026, 9, 11, 12)) is None


# --- which slot "now" belongs to ----------------------------------------------


def test_the_current_slot_is_the_one_just_passed():
    slot = scheduler.current_slot(on_a_timer(), manila(2026, 9, 11, 11, 5))
    assert slot.strftime("%H:%M") == "11:00"


def test_a_restart_owes_the_feed_one_fresh_post_not_a_backlog():
    """The property the whole design turns on.

    A scheduler coming up at 15:10 has missed 09:00, 11:00 and 13:00. Returning
    the earliest unfilled slot would draft all three, one after another, and
    put three stale posts in a row into a live feed. It owes exactly one: 15:00.
    """
    slot = scheduler.current_slot(on_a_timer(), manila(2026, 9, 11, 15, 10))
    assert slot.strftime("%H:%M") == "15:00"


def test_a_slot_missed_by_more_than_the_grace_is_skipped():
    """A 09:00 brief written at 11:30 is not the 09:00 post arriving late -- it
    is a stale post competing with the 11:00 one."""
    brand = on_a_timer(max_per_day=1)  # 09:00 only, so nothing else is elapsed
    late = manila(2026, 9, 11, 9) + scheduler.CATCH_UP_GRACE + timedelta(minutes=1)

    assert scheduler.current_slot(brand, late) is None


def test_a_slot_inside_the_grace_still_counts():
    brand = on_a_timer(max_per_day=1)
    late = manila(2026, 9, 11, 9) + scheduler.CATCH_UP_GRACE - timedelta(minutes=1)

    assert scheduler.current_slot(brand, late).strftime("%H:%M") == "09:00"


def test_before_the_first_slot_nothing_is_due():
    assert scheduler.current_slot(on_a_timer(), manila(2026, 9, 11, 7)) is None


# --- how long the reviewer gets -----------------------------------------------


def test_the_reviewer_gets_most_of_the_gap_to_the_next_slot():
    """90 minutes at a 2h cadence -- the gap, less the margin that keeps two
    live drafts for one brand off one person's screen."""
    seconds = scheduler.approval_window(on_a_timer(), manila(2026, 9, 11, 9))

    expected = timedelta(hours=2) - scheduler.APPROVAL_MARGIN
    assert seconds == int(expected.total_seconds()) == 5400


def test_the_evening_draft_does_not_get_all_night():
    """Capped at one slot's worth, which only matters for the last slot.

    The gap from 17:00 to tomorrow's 09:00 is sixteen hours. Uncapped, the
    evening draft would be the one post approvable at breakfast and published a
    day stale -- an expired draft costs a search, a stale post costs the feed.
    """
    seconds = scheduler.approval_window(on_a_timer(), manila(2026, 9, 11, 17))

    assert seconds == 5400


def test_the_window_never_falls_below_the_default():
    """Ten minutes is a floor, not a starting point: a slot drafted late still
    needs long enough for somebody to read it."""
    just_before = manila(2026, 9, 11, 11) - timedelta(seconds=30)
    seconds = scheduler.approval_window(on_a_timer(), just_before)

    assert seconds == scheduler.DEFAULT_TIMEOUT_SECONDS


def test_a_brand_with_no_slots_gets_the_default_window():
    by_hand = make_brand(every_hours=0, first_slot=None)
    assert scheduler.approval_window(by_hand, manila(2026, 9, 11, 12)) == (
        scheduler.DEFAULT_TIMEOUT_SECONDS
    )


# --- how long to sleep --------------------------------------------------------


def test_sleep_is_bounded_however_far_off_the_next_slot_is():
    """A laptop that suspends for three hours wakes with a timer that never
    fired; a short leash turns that into a slot that is merely late."""
    overnight = scheduler.sleep_seconds([on_a_timer()], manila(2026, 9, 11, 20))
    assert overnight == scheduler.MAX_SLEEP_SECONDS


def test_sleep_stops_at_the_next_slot():
    delay = scheduler.sleep_seconds([on_a_timer()], manila(2026, 9, 11, 10, 55))
    assert delay == pytest.approx(300, abs=1)


def test_sleep_is_never_zero():
    """A slot landing exactly now would otherwise spin the loop."""
    delay = scheduler.sleep_seconds([on_a_timer()], manila(2026, 9, 11, 11))
    assert delay >= 1.0


# --- is this slot already drafted ---------------------------------------------


@pytest.fixture
def drafts(monkeypatch):
    """Stand in for the store: `drafts_since` returns whatever is set here."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_transaction():
        yield object()

    monkeypatch.setattr(scheduler, "transaction", fake_transaction)

    async def fake_drafts_since(_session, *, brand_slug, since):
        fake_drafts_since.asked = since
        return fake_drafts_since.count

    fake_drafts_since.count = 0
    fake_drafts_since.asked = None
    monkeypatch.setattr(scheduler, "drafts_since", fake_drafts_since)
    return fake_drafts_since


async def test_a_slot_with_no_draft_yet_is_due(drafts):
    drafts.count = 0
    assert await scheduler.is_due(on_a_timer(), manila(2026, 9, 11, 11, 5)) is True


async def test_a_slot_already_drafted_is_not_due_again(drafts):
    """Answered from the database rather than a flag in this process, so a
    scheduler restarted at 11:05 asks what the one that died at 11:01 asked."""
    drafts.count = 1
    assert await scheduler.is_due(on_a_timer(), manila(2026, 9, 11, 11, 5)) is False


async def test_the_question_is_scoped_to_the_slot_not_the_day(drafts):
    """`since` is the slot's own start, or the second slot of the day would
    always look already-drafted."""
    await scheduler.is_due(on_a_timer(), manila(2026, 9, 11, 11, 5))
    assert drafts.asked.astimezone(ZoneInfo(MANILA)).strftime("%H:%M") == "11:00"


async def test_nothing_is_due_outside_a_slot(drafts):
    drafts.count = 0
    assert await scheduler.is_due(on_a_timer(), manila(2026, 9, 11, 7)) is False


# --- the cycle ----------------------------------------------------------------


@pytest.fixture
def fired(monkeypatch):
    """Record which brands `tick` started a run for, without running one."""
    started: list[str] = []

    async def fake_fire(brand, _now=None):
        started.append(brand.slug)

    monkeypatch.setattr(scheduler, "fire", fake_fire)
    return started


@pytest.fixture
def due(monkeypatch):
    """Every brand is due, unless a test says otherwise."""
    async def always(_brand, _now):
        return True

    monkeypatch.setattr(scheduler, "is_due", always)


async def test_a_due_brand_is_fired(fired, due):
    import asyncio

    started = await scheduler.tick([on_a_timer()], manila(2026, 9, 11, 11, 5))
    await asyncio.gather(*tuple(scheduler._tasks))

    assert started == 1
    assert fired == ["pinoysing"]


async def test_a_brand_still_waiting_on_its_last_approval_skips_its_slot(fired, due):
    """A run blocks on a human for most of an hour, so the loop comes back
    around while the first is still at the gate. Without this the reviewer gets
    two live drafts for one brand at once."""
    scheduler._running.add("pinoysing")

    started = await scheduler.tick([on_a_timer()], manila(2026, 9, 11, 11, 5))

    assert started == 0
    assert fired == []


async def test_one_brands_slow_reviewer_does_not_cost_another_brand_its_slot(
    fired, due
):
    import asyncio

    scheduler._running.add("pinoysing")
    brands = [on_a_timer(), on_a_timer(slug="derekt")]

    started = await scheduler.tick(brands, manila(2026, 9, 11, 11, 5))
    await asyncio.gather(*tuple(scheduler._tasks))

    assert started == 1
    assert fired == ["derekt"]


async def test_the_kill_switch_stops_drafting(fired, due, monkeypatch, tmp_path):
    """FR-15, for drafting. Checked every cycle, so it takes effect without a
    restart -- the moment you want it is the moment you do not want one."""
    switch = tmp_path / "PAUSE_DRAFTING"
    switch.touch()
    monkeypatch.setattr(scheduler, "KILL_SWITCH", switch)

    assert await scheduler.tick([on_a_timer()], manila(2026, 9, 11, 11, 5)) == 0
    assert fired == []


async def test_a_spawned_run_is_held_while_it_waits(fired, due):
    """asyncio keeps only a weak reference; a run collected mid-approval would
    vanish without an error."""
    import asyncio

    await scheduler.tick([on_a_timer()], manila(2026, 9, 11, 11, 5))
    assert scheduler._tasks, "the spawned run is unreferenced"

    await asyncio.gather(*tuple(scheduler._tasks))
    assert not scheduler._tasks, "a finished run must not be held forever"


async def test_fire_clears_the_running_flag_even_when_the_run_fails(monkeypatch):
    """`_running` is what stops a second draft for the same brand. A run that
    raised without clearing it would freeze that brand until a restart."""
    async def explode(_brand, approval_timeout=None):
        raise RuntimeError("the database is down")

    monkeypatch.setattr(scheduler, "brief_run", explode)

    with pytest.raises(RuntimeError):
        await scheduler.fire(on_a_timer(), manila(2026, 9, 11, 11))

    assert "pinoysing" not in scheduler._running
