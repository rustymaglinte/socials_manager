"""app.domain.brand -- loading, normalisation, and channel routing.

`brand_for_channel` is the whole of D3's enforcement (SPECS D3), so the routing
tests here matter more than their size suggests: a wrong answer puts one brand's
draft in another brand's channel.
"""

from datetime import time

import pytest

from app.domain.brand import (
    PLACEHOLDER,
    Account,
    BrandMisconfigured,
    BrandNotFound,
    account_id_env_var,
    all_brands,
    brand_for_channel,
    load_brand,
    normalise_channel,
    normalise_hashtag,
)
from tests.conftest import DEREKT_YAML, PERSONAL_YAML, PINOYSING_BRIEFS


def test_the_shipped_brand_files_all_load(real_brands):
    """A smoke test over brands/ as committed, not a tmp_path fixture.

    These are hand-edited YAML that nothing else in the suite reads, so a syntax
    error or a bad timezone in one would otherwise surface at runtime. Says
    nothing about angles being written yet -- a template with none is valid.
    """
    for brand in real_brands:
        assert brand.display_name
        assert PLACEHOLDER not in brand.briefs.shared
        for name, text in brand.briefs.angles.items():
            assert PLACEHOLDER not in text, f"{brand.slug}/{name} shipped a TODO"


def test_pinoysing_posts_at_noon_and_seven_in_the_evening(real_brands):
    """Twice a day, Manila time: 1-2 posts/day is the Facebook Page norm, and
    karaoke is an evening habit. Three hours to approve, so a late approval
    cannot publish after midnight."""
    brand = next(b for b in real_brands if b.slug == "pinoysing")

    assert brand.timezone == "Asia/Manila"
    assert brand.post_slots == (time(12, 0), time(19, 0))
    assert brand.max_per_day == 2
    assert brand.max_per_week == 14
    assert brand.approval_hours == 3


def test_cadence_reads_approval_hours(write_brand):
    write_brand(
        "timed",
        """
        display_name: "Timed"
        cadence:
          max_per_day: 2
          every_hours: 7
          first_slot: "12:00"
          approval_hours: 3
        """,
    )

    assert load_brand("timed").approval_hours == 3


def test_approval_hours_defaults_to_zero(write_brand):
    """Zero means "the gap to the next slot decides", which is today's behaviour."""
    write_brand("bare", 'display_name: "Bare"')

    assert load_brand("bare").approval_hours == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#Derekt-Socials", "derekt-socials"),
        ("derekt-socials", "derekt-socials"),
        ("  #DEREKT-SOCIALS  ", "derekt-socials"),
        ("", ""),
    ],
)
def test_normalise_channel(raw, expected):
    assert normalise_channel(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("#trading", "#trading"), ("trading", "#trading"), ("  quant  ", "#quant")],
)
def test_normalise_hashtag(raw, expected):
    assert normalise_hashtag(raw) == expected


@pytest.mark.parametrize(
    ("external_id", "configured"),
    [("12345", True), ("TODO", False), (None, False), ("", False)],
)
def test_account_configured_rejects_the_template_placeholder(external_id, configured):
    account = Account(platform="linkedin", enabled=True, external_id=external_id)
    assert account.configured is configured


def test_load_brand_reads_every_field(write_brand):
    write_brand("derekt", DEREKT_YAML, voice="Dry, numbers first.")

    brand = load_brand("derekt")

    assert brand.slug == "derekt"
    assert brand.display_name == "Derekt"
    assert brand.tier == "brand"
    assert brand.policy_profile == "standard"
    assert brand.slack_channel == "derekt-socials"  # '#Derekt-Socials' normalised
    assert brand.voice == "Dry, numbers first."
    assert brand.banned_terms == ("guaranteed returns", "risk-free")
    assert brand.required_disclaimers == {
        "performance": "Past performance does not predict future results.",
        "advice": "Not financial advice.",
    }
    assert brand.max_per_day == 2
    assert brand.max_per_week == 10


def test_load_brand_normalises_hashtags_from_yaml(write_brand):
    write_brand("derekt", DEREKT_YAML)

    # yaml mixes '#trading' with bare 'tradingbot'; the prompt needs all hashed.
    assert load_brand("derekt").hashtags == ("#trading", "#tradingbot", "#quant")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("facebook", "FB_PAGE_ID_PINOYSING"),
        ("x", "X_HANDLE_PINOYSING"),
        ("youtube", "YOUTUBE_CHANNEL_ID_PINOYSING"),
        ("linkedin", "LINKEDIN_ID_PINOYSING"),
    ],
)
def test_account_id_env_var_sits_beside_the_token_variable(platform, expected):
    """Same shape as FB_PAGE_TOKEN_<SLUG>, so a Page's id and token are one
    search apart in .env and in Railway's variable list."""
    assert account_id_env_var("pinoysing", platform) == expected


def test_account_id_env_var_is_none_for_a_platform_with_no_convention():
    assert account_id_env_var("pinoysing", "tiktok") is None


ALL_PLATFORMS_YAML = """
display_name: "Mixed"
accounts:
  - platform: linkedin
    enabled: true
  - platform: facebook
    enabled: true
  - platform: youtube
    enabled: true
  - platform: x
    enabled: true
"""


def test_load_brand_reads_each_account_id_from_the_environment(write_brand, monkeypatch):
    """Ids are deployment state, not brand identity: the live and test Page
    differ per environment, the same argument that put Slack channel ids in .env."""
    monkeypatch.setenv("LINKEDIN_ID_MIXED", "rusty")
    monkeypatch.setenv("FB_PAGE_ID_MIXED", "67890")
    monkeypatch.setenv("YOUTUBE_CHANNEL_ID_MIXED", "UC123")
    write_brand("mixed", ALL_PLATFORMS_YAML)

    ids = {a.platform: a.external_id for a in load_brand("mixed").accounts}
    assert ids == {
        "linkedin": "rusty",
        "facebook": "67890",
        "youtube": "UC123",
        "x": None,  # X_HANDLE_MIXED is unset
    }


def test_an_unset_id_variable_leaves_the_account_unconfigured(write_brand):
    """Draftable nowhere, publishable nowhere -- exactly what a TODO used to mean."""
    write_brand("mixed", ALL_PLATFORMS_YAML)

    brand = load_brand("mixed")

    assert all(not account.configured for account in brand.accounts)
    assert brand.publishable_accounts == ()


def test_a_blank_id_variable_is_the_same_as_unset(write_brand, monkeypatch):
    monkeypatch.setenv("FB_PAGE_ID_MIXED", "   ")
    write_brand("mixed", ALL_PLATFORMS_YAML)

    facebook = next(a for a in load_brand("mixed").accounts if a.platform == "facebook")
    assert facebook.external_id is None


def test_an_unknown_platform_loads_without_an_id(write_brand, caplog):
    write_brand(
        "odd",
        """
        display_name: "Odd"
        accounts:
          - platform: tiktok
            enabled: true
        """,
    )

    (account,) = load_brand("odd").accounts

    assert account.external_id is None
    assert "tiktok" in caplog.text


@pytest.mark.parametrize("key", ["page_id", "handle", "channel_id", "external_id"])
@pytest.mark.parametrize("value", ["111111111111111", "TODO"])
def test_an_id_left_in_brand_yaml_refuses_to_load(write_brand, monkeypatch, key, value):
    """Refused rather than ignored. Ignoring it is the dangerous half: someone
    swaps to the test Page in the yaml, nothing complains, and the post goes to
    the live Page the environment still names."""
    monkeypatch.setenv("FB_PAGE_ID_STALE", "2222222222222222")
    write_brand(
        "stale",
        f"""
        display_name: "Stale"
        accounts:
          - platform: facebook
            {key}: "{value}"
            enabled: true
        """,
    )

    with pytest.raises(BrandMisconfigured, match="FB_PAGE_ID_STALE") as raised:
        load_brand("stale")
    assert key in str(raised.value)


def test_load_brand_defaults_everything_optional(write_brand):
    write_brand("bare", 'display_name: "Bare"')

    brand = load_brand("bare")

    assert brand.slug == "bare"  # falls back to the directory name
    assert brand.tier == "brand"
    assert brand.policy_profile == "standard"
    assert brand.slack_channel == ""
    assert brand.accounts == ()
    assert brand.banned_terms == ()
    assert brand.required_disclaimers == {}
    assert brand.hashtags == ()
    assert brand.max_per_day == 0
    assert brand.max_per_week == 0


def test_load_brand_without_brand_yaml_raises(brands_dir):
    (brands_dir / "ghost").mkdir()

    with pytest.raises(BrandNotFound):
        load_brand("ghost")


def test_missing_voice_is_absent_rather_than_fatal(write_brand):
    write_brand("derekt", DEREKT_YAML)  # no voice.md written

    assert load_brand("derekt").voice is None


def test_template_voice_is_treated_as_no_voice(write_brand):
    """A TODO rendered into the prompt is worse than no voice profile at all."""
    write_brand("derekt", DEREKT_YAML, voice="# Voice\n\nTODO: describe the voice.")

    assert load_brand("derekt").voice is None


def test_briefs_load_from_the_brand_directory(write_brand):
    write_brand("pinoysing", 'display_name: "PinoySing"', briefs=PINOYSING_BRIEFS)

    catalog = load_brand("pinoysing").briefs

    assert catalog.angle_names == ("trending_music", "trivia_music")
    assert "Ilimit up to 130 characters" in catalog.shared
    # The placeholder survives loading; it is filled per run, not per read.
    assert "{today}" in catalog.angles["trending_music"]


def test_a_brand_without_briefs_yaml_gets_an_empty_catalog(write_brand):
    """Briefable by hand is a valid state; it must not be a load failure."""
    write_brand("derekt", DEREKT_YAML)

    assert load_brand("derekt").briefs.angle_names == ()


def test_a_todo_angle_is_skipped_rather_than_briefed(write_brand):
    write_brand(
        "half",
        'display_name: "Half"',
        briefs="""
        angles:
          ready: "Search for karaoke trivia and draft a post."
          unwritten: "TODO: decide what this angle asks for."
          blank: ""
        """,
    )

    assert load_brand("half").briefs.angle_names == ("ready",)


def test_a_todo_shared_block_is_dropped_even_when_an_angle_is_ready(write_brand):
    """The shared block reaches every brief, so a TODO there leaks into all of them."""
    write_brand(
        "half",
        'display_name: "Half"',
        briefs="""
        shared: "TODO: decide the rules every post follows."
        angles:
          ready: "Search for karaoke trivia and draft a post."
        """,
    )

    catalog = load_brand("half").briefs

    assert catalog.shared == ""
    assert catalog.angle_names == ("ready",)  # the usable angle survives


def test_timezone_defaults_to_utc_when_undeclared(write_brand):
    write_brand("derekt", DEREKT_YAML)

    assert load_brand("derekt").timezone == "UTC"


def test_declared_timezone_is_kept(write_brand):
    write_brand("pinoysing", 'display_name: "PinoySing"\ntimezone: "Asia/Manila"')

    assert load_brand("pinoysing").timezone == "Asia/Manila"


def test_an_unknown_timezone_fails_at_load_not_mid_run(write_brand):
    write_brand("typo", 'display_name: "Typo"\ntimezone: "Asia/Manilla"')

    with pytest.raises(BrandMisconfigured, match="Asia/Manilla"):
        load_brand("typo")


def test_enabled_accounts_filters_disabled_ones(write_brand):
    write_brand("personal", PERSONAL_YAML)

    brand = load_brand("personal")

    assert len(brand.accounts) == 2
    assert [a.platform for a in brand.enabled_accounts] == ["linkedin"]


def test_load_brand_is_cached(write_brand):
    """A stable prompt prefix needs byte-stable input, so this must not re-read."""
    write_brand("derekt", DEREKT_YAML)

    assert load_brand("derekt") is load_brand("derekt")


def test_all_brands_is_sorted_and_skips_directories_without_config(
    two_brands, brands_dir
):
    (brands_dir / "notabrand").mkdir()  # a stray directory, no brand.yaml

    assert [brand.slug for brand in all_brands()] == ["derekt", "personal"]


def test_all_brands_without_a_brands_directory_raises(brands_dir):
    brands_dir.rmdir()

    with pytest.raises(BrandNotFound):
        all_brands()


@pytest.mark.parametrize(
    "channel", ["derekt-socials", "#derekt-socials", "#Derekt-Socials", "  DEREKT-SOCIALS "]
)
def test_brand_for_channel_matches_however_the_channel_is_written(two_brands, channel):
    assert brand_for_channel(channel).slug == "derekt"


def test_brand_for_channel_keeps_brands_apart(two_brands):
    assert brand_for_channel("#personal-socials").slug == "personal"
    assert brand_for_channel("#derekt-socials").slug == "derekt"


def test_brand_for_unknown_channel_raises_rather_than_guessing(two_brands):
    with pytest.raises(BrandNotFound):
        brand_for_channel("#random-watercooler")
