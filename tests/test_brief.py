"""app.agent.brief -- assembling the run's opening turn.

The date tests carry most of the weight here. The version this replaced built
its briefs as module-level f-strings, which froze the date at import: a process
holding a Slack socket open for a week briefed the model with the day it booted.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent.brief import BriefError, build_brief, pick_angle, today
from app.domain.brand import BriefCatalog
from tests.conftest import make_brand, make_catalog

# 26 hours apart, so their calendar dates differ at every instant. Lets the
# timezone assertions below be exact without freezing the clock.
AHEAD = "Pacific/Kiritimati"  # UTC+14
BEHIND = "Etc/GMT+12"  # UTC-12


def brand_with(catalog: BriefCatalog | None = None, **overrides):
    return make_brand(briefs=catalog or make_catalog(), **overrides)


def test_today_is_read_in_the_brands_timezone_not_the_hosts():
    ahead = today(brand_with(timezone=AHEAD))
    behind = today(brand_with(timezone=BEHIND))

    assert ahead != behind
    assert ahead == datetime.now(ZoneInfo(AHEAD)).strftime("%B %d, %Y")


def test_the_date_is_resolved_per_call_rather_than_at_import():
    """The regression the f-string dict shipped: one evaluation, reused forever."""
    brand = brand_with(timezone=AHEAD)
    expected = datetime.now(ZoneInfo(AHEAD)).strftime("%B %d, %Y")

    assert expected in build_brief(brand, "trending")


def test_build_brief_appends_the_shared_rules_to_the_angle():
    brief = build_brief(brand_with(), "trivia")

    assert "karaoke trivia" in brief
    assert "under 130 characters" in brief


def test_no_placeholder_survives_into_the_finished_brief():
    for angle in ("trivia", "trending"):
        assert "{" not in build_brief(brand_with(), angle)


def test_a_catalog_with_no_shared_block_still_builds():
    catalog = make_catalog(shared="")

    assert build_brief(brand_with(catalog), "trivia").strip()


def test_an_unknown_angle_names_the_ones_that_exist():
    with pytest.raises(BriefError, match="trending"):
        build_brief(brand_with(), "no_such_angle")


def test_an_unknown_placeholder_is_named_rather_than_crashing_obscurely():
    catalog = make_catalog(angles={"broken": "Post about {weather} today."})

    with pytest.raises(BriefError, match="weather"):
        build_brief(brand_with(catalog), "broken")


def test_recent_topics_become_a_do_not_repeat_block():
    brief = build_brief(brand_with(), "trivia", recent=["empty orchestra", "Sinatra"])

    assert "Huwag ulitin" in brief
    assert "- empty orchestra" in brief
    assert "- Sinatra" in brief


def test_no_recent_topics_means_no_block_at_all():
    assert "Huwag ulitin" not in build_brief(brand_with(), "trivia")


def test_recent_posts_are_not_treated_as_format_strings():
    """Real post text can contain a brace, and it is not ours to interpolate."""
    brief = build_brief(brand_with(), "trivia", recent=["a post with {braces} in it"])

    assert "{braces}" in brief


def test_pick_angle_avoids_what_was_just_used():
    # One angle left unused, so the choice is forced whatever the rng does.
    assert pick_angle(brand_with(), recent=["trending"]) == "trivia"


def test_pick_angle_falls_back_once_every_angle_is_recent():
    """A two-angle brand on its third run still has to be briefed somehow."""
    angle = pick_angle(brand_with(), recent=["trivia", "trending"])

    assert angle in {"trivia", "trending"}


def test_pick_angle_ignores_recent_names_the_brand_no_longer_has():
    assert pick_angle(brand_with(), recent=["retired_angle", "trending"]) == "trivia"


def test_pick_angle_without_a_catalog_raises():
    with pytest.raises(BriefError, match="no brief angles"):
        pick_angle(brand_with(BriefCatalog()))


def test_angle_names_are_sorted_so_selection_is_reproducible():
    catalog = make_catalog(angles={"zeta": "z", "alpha": "a", "mid": "m"})

    assert catalog.angle_names == ("alpha", "mid", "zeta")
