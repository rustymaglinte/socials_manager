"""Shared fixtures.

Two things have to happen before any `app.*` module is imported, and a conftest
is the only place early enough for both:

1. Fake Slack credentials. `client.py` builds its AsyncApp at import time from
   SLACK_BOT_TOKEN, so the variable must exist or importing the transport fails.
   Set here rather than in .env so the suite never holds a real token -- these
   go in first, and `load_dotenv()` does not override what is already set.
2. Nothing else: brands are repointed per test, since `load_brand` is cached and
   a leaked cache entry would make test order matter.
"""

import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-a-real-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-a-real-token")
os.environ.setdefault("TAVILY_API_KEY", "test-not-a-real-key")

import textwrap
from pathlib import Path

import pytest

from app.domain import brand as brand_module
from app.domain.brand import Account, BrandContext

PERSONAL_YAML = """
slug: personal
display_name: "Rusty Maglinte"
tier: personal
policy_profile: personal
slack_channel: "#personal-socials"

accounts:
  - platform: linkedin
    handle: "rusty"
    enabled: true
  - platform: x
    handle: "TODO"
    enabled: false

cadence:
  max_per_day: 1
  max_per_week: 3
"""

DEREKT_YAML = """
slug: derekt
display_name: "Derekt"
tier: brand
policy_profile: standard
slack_channel: "#Derekt-Socials"

accounts:
  - platform: linkedin
    page_id: "12345"
    enabled: true
  - platform: facebook
    page_id: "67890"
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


def _clear_brand_caches() -> None:
    brand_module.load_brand.cache_clear()
    brand_module.all_brands.cache_clear()


@pytest.fixture
def brands_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty brands/ that `app.domain.brand` reads from, caches cleared.

    Cleared on the way out as well as in: `load_brand` is process-wide, so a
    surviving entry would point later tests at a tmp_path that no longer exists.
    """
    directory = tmp_path / "brands"
    directory.mkdir()
    monkeypatch.setattr(brand_module, "BRANDS_DIR", directory)
    _clear_brand_caches()
    yield directory
    _clear_brand_caches()


@pytest.fixture
def write_brand(brands_dir: Path):
    """write_brand("personal", PERSONAL_YAML, voice="...") -> the brand directory."""

    def _write(slug: str, config: str | None, voice: str | None = None) -> Path:
        directory = brands_dir / slug
        directory.mkdir(parents=True, exist_ok=True)
        if config is not None:
            directory.joinpath("brand.yaml").write_text(
                textwrap.dedent(config), encoding="utf-8"
            )
        if voice is not None:
            directory.joinpath("voice.md").write_text(voice, encoding="utf-8")
        return directory

    return _write


@pytest.fixture
def two_brands(write_brand) -> None:
    """The pair the isolation rules are about: one personal, one commercial."""
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
