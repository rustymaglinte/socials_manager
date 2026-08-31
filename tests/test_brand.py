"""app.domain.brand -- loading, normalisation, and channel routing.

`brand_for_channel` is the whole of D3's enforcement (SPECS D3), so the routing
tests here matter more than their size suggests: a wrong answer puts one brand's
draft in another brand's channel.
"""

import pytest

from app.domain.brand import (
    Account,
    BrandNotFound,
    all_brands,
    brand_for_channel,
    load_brand,
    normalise_channel,
    normalise_hashtag,
)
from tests.conftest import DEREKT_YAML, PERSONAL_YAML


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


def test_load_brand_reads_the_id_under_whichever_key_the_platform_uses(write_brand):
    write_brand(
        "mixed",
        """
        display_name: "Mixed"
        accounts:
          - platform: linkedin
            handle: "rusty"
            enabled: true
          - platform: facebook
            page_id: "67890"
            enabled: true
          - platform: youtube
            channel_id: "UC123"
            enabled: true
          - platform: x
            enabled: true
        """,
    )

    ids = {a.platform: a.external_id for a in load_brand("mixed").accounts}
    assert ids == {
        "linkedin": "rusty",
        "facebook": "67890",
        "youtube": "UC123",
        "x": None,  # no identifier key at all
    }


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
