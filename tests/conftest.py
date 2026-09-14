"""Shared fixtures.

Two things have to happen before any `app.*` module is imported, and a conftest
is the only place early enough for both:

1. Fake Slack credentials. `client.py` builds its AsyncApp at import time from
   SLACK_BOT_TOKEN, so the variable must exist or importing the transport fails.
   Set here rather than in .env so the suite never holds a real token -- these
   go in first, and `load_dotenv()` does not override what is already set.
2. Nothing else: brands are repointed per test, since `load_brand` is cached and
   a leaked cache entry would make test order matter.

Account ids are the opposite case: they must *not* come from the real .env,
which `client.py`'s `load_dotenv()` pulls into this process on import. See
`_no_real_account_ids`.
"""

import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-a-real-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-a-real-token")
os.environ.setdefault("TAVILY_API_KEY", "test-not-a-real-key")

import textwrap
from pathlib import Path

import pytest

from app.domain.brand import ACCOUNT_ID_PREFIX, Account, BrandContext, BriefCatalog

# The loader module, not the package facade: `app.domain.brand.BRANDS_DIR` is a
# copy bound at import, so patching it there would leave the loader reading the
# real brands/ directory.
from app.domain.brand import loader as brand_loader

PINOYSING_BRIEFS = """
shared: |
  Sundin ang voice ng brand na ito.
  Ilimit up to 130 characters ang iyong post.

angles:
  trivia_music: |
    Mag search ng trivia about karaoke at gumawa ng isang facebook post.
  trending_music: |
    Mag search ng trending topics ngayong araw, {today}, at gumawa ng post.
"""

PERSONAL_YAML = """
slug: personal
display_name: "Rusty Maglinte"
tier: personal
policy_profile: personal
slack_channel: "#personal-socials"

accounts:
  - platform: linkedin
    enabled: true
  - platform: x
    enabled: false

cadence:
  max_per_day: 1
  max_per_week: 3
"""

# What `two_brands` puts in the environment. The ids these brands had in their
# yaml before ids moved out of it, so the tests reading them did not change.
TWO_BRANDS_ACCOUNT_IDS = {
    "LINKEDIN_ID_PERSONAL": "rusty",
    "LINKEDIN_ID_DEREKT": "12345",
    "FB_PAGE_ID_DEREKT": "67890",
}

DEREKT_YAML = """
slug: derekt
display_name: "Derekt"
tier: brand
policy_profile: standard
slack_channel: "#Derekt-Socials"

accounts:
  - platform: linkedin
    enabled: true
  - platform: facebook
    enabled: true

banned_terms:
  - "guaranteed returns"
  - "risk-free"

required_disclaimers:
  performance: "Past performance does not predict future results."
  advice: "Not financial advice."

hashtags:
  default: ["#trading", "tradingbot", "quant"]

cadence:
  max_per_day: 2
  max_per_week: 10
"""


# Captured before any test repoints it, so `real_brands` can point back.
REAL_BRANDS_DIR = brand_loader.BRANDS_DIR


def _clear_brand_caches() -> None:
    brand_loader.load_brand.cache_clear()
    brand_loader.all_brands.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_account_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no account id in the environment.

    The loader reads ids from the environment, and the developer's .env holds
    the live PinoySing Page id. Left in, a test's outcome would depend on whose
    machine ran it -- and a test that means "an unset id is unpublishable" would
    pass on CI and fail on the laptop that can actually post. A test that needs
    an id sets it.
    """
    prefixes = tuple(f"{prefix}_" for prefix in ACCOUNT_ID_PREFIX.values())
    for name in list(os.environ):
        if name.startswith(prefixes):
            monkeypatch.delenv(name)


@pytest.fixture
def brands_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty brands/ that `app.domain.brand` reads from, caches cleared.

    Cleared on the way out as well as in: `load_brand` is process-wide, so a
    surviving entry would point later tests at a tmp_path that no longer exists.
    """
    directory = tmp_path / "brands"
    directory.mkdir()
    monkeypatch.setattr(brand_loader, "BRANDS_DIR", directory)
    _clear_brand_caches()
    yield directory
    _clear_brand_caches()


@pytest.fixture
def write_brand(brands_dir: Path):
    """write_brand("personal", PERSONAL_YAML, voice="...") -> the brand directory."""

    def _write(
        slug: str,
        config: str | None,
        voice: str | None = None,
        briefs: str | None = None,
    ) -> Path:
        directory = brands_dir / slug
        directory.mkdir(parents=True, exist_ok=True)
        if config is not None:
            directory.joinpath("brand.yaml").write_text(
                textwrap.dedent(config), encoding="utf-8"
            )
        if voice is not None:
            directory.joinpath("voice.md").write_text(voice, encoding="utf-8")
        if briefs is not None:
            directory.joinpath("briefs.yaml").write_text(
                textwrap.dedent(briefs), encoding="utf-8"
            )
        return directory

    return _write


@pytest.fixture
def real_brands(monkeypatch: pytest.MonkeyPatch):
    """brands/ as committed, for the one test that checks the shipped files.

    Everything else repoints BRANDS_DIR at a tmp_path. This deliberately does
    not, so the caches are cleared on both sides here too -- a real entry left
    behind would satisfy a later test that meant to read its own fixture.
    """
    monkeypatch.setattr(brand_loader, "BRANDS_DIR", REAL_BRANDS_DIR)
    _clear_brand_caches()
    yield brand_loader.all_brands()
    _clear_brand_caches()


@pytest.fixture
def two_brands(write_brand, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pair the isolation rules are about: one personal, one commercial."""
    for name, value in TWO_BRANDS_ACCOUNT_IDS.items():
        monkeypatch.setenv(name, value)
    write_brand("personal", PERSONAL_YAML, voice="# Voice\n\nPlain and direct.")
    write_brand("derekt", DEREKT_YAML, voice="# Voice\n\nDry, numbers first.")


def make_brand(**overrides) -> BrandContext:
    """A BrandContext built in memory, for tests with no reason to touch disk."""
    defaults = {
        "slug": "derekt",
        "display_name": "Derekt",
        "tier": "brand",
        "policy_profile": "standard",
        "slack_channel": "derekt-socials",
        "accounts": (
            Account(platform="linkedin", enabled=True, external_id="12345"),
            Account(platform="facebook", enabled=True, external_id="67890"),
            Account(platform="x", enabled=False, external_id="99999"),
        ),
        "voice": "Dry, numbers first.",
        "banned_terms": ("guaranteed returns",),
        "required_disclaimers": {"performance": "Past performance is not a guide."},
        "hashtags": ("#trading", "#quant"),
        "max_per_day": 2,
        "max_per_week": 10,
    }
    return BrandContext(**{**defaults, **overrides})


def make_catalog(**overrides) -> BriefCatalog:
    """A two-angle catalog, one of which needs today's date."""
    defaults = {
        "shared": "Keep it under 130 characters.",
        "angles": {
            "trivia": "Search for karaoke trivia and draft a post.",
            "trending": "Search for what is trending today, {today}, and draft a post.",
        },
    }
    return BriefCatalog(**{**defaults, **overrides})
