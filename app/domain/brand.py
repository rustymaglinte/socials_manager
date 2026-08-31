"""Brands, loaded from brands/<slug>/.

Framework-free by contract: stdlib and PyYAML only. What differs between brands
is voice, cadence, and policy -- data, not code (SPECS D1).
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# brands/ sits beside app/ at the repo root. Module-level so tests can repoint it.
BRANDS_DIR = Path(__file__).resolve().parents[2] / "brands"

# Both brand.yaml and voice.md ship as templates full of TODOs. A TODO rendered
# into a prompt is worse than nothing, so it is detected rather than trusted.
PLACEHOLDER = "TODO"

# Platforms name their identifier differently; the loader only needs to find one.
_ID_KEYS = ("handle", "page_id", "channel_id", "external_id")


class BrandNotFound(LookupError):
    """No such brand directory, or no brand bound to that channel."""


@dataclass(frozen=True)
class Account:
    platform: str
    enabled: bool
    external_id: str | None

    @property
    def configured(self) -> bool:
        """False while the yaml still says TODO -- draftable, but not publishable."""
        return bool(self.external_id) and self.external_id != PLACEHOLDER


@dataclass(frozen=True)
class BrandContext:
    """One brand, resolved before the model is invoked (SPECS D3).

    Never a tool argument and never derived from model output: a parameter is
    something the model can get wrong.
    """

    slug: str
    display_name: str
    tier: str
    policy_profile: str
    slack_channel: str  # normalised: no leading '#', lowercase
    accounts: tuple[Account, ...]
    voice: str | None  # None while voice.md is still the TODO template
    banned_terms: tuple[str, ...]
    required_disclaimers: dict[str, str]
    hashtags: tuple[str, ...]
    max_per_day: int
    max_per_week: int

    @property
    def enabled_accounts(self) -> tuple[Account, ...]:
        return tuple(account for account in self.accounts if account.enabled)


def normalise_channel(channel: str) -> str:
    """`#Derekt-Socials` and `derekt-socials` are the same channel."""
    return channel.strip().lstrip("#").lower()


def normalise_hashtag(tag: str) -> str:
    """Add the missing '#'. brand.yaml mixes `#trading` and `tradingbot`, and a
    tag without its hash reads as an ordinary word once it is in the prompt."""
    tag = tag.strip()
    return tag if tag.startswith("#") else f"#{tag}"


def _account(raw: dict[str, Any]) -> Account:
    external_id = next((raw[key] for key in _ID_KEYS if raw.get(key)), None)
    return Account(
        platform=raw["platform"],
        enabled=bool(raw.get("enabled", False)),
        external_id=external_id,
    )


def _load_voice(path: Path, slug: str) -> str | None:
    if not path.is_file():
        logger.warning("No voice.md for %s; drafting without a voice profile", slug)
        return None

    text = path.read_text(encoding="utf-8").strip()
    if PLACEHOLDER in text:
        logger.warning(
            "voice.md for %s is still the TODO template (SPECS Q3); "
            "drafting without a voice profile",
            slug,
        )
        return None
    return text


@lru_cache(maxsize=None)
def load_brand(slug: str) -> BrandContext:
    """Read brands/<slug>/.

    Cached deliberately: a stable prompt prefix needs stable input, and
    re-reading per turn would invalidate the provider cache for no gain.
    Restart the process after editing a brand.
    """
    directory = BRANDS_DIR / slug
    config_path = directory / "brand.yaml"
    if not config_path.is_file():
        raise BrandNotFound(f"No brand.yaml at {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cadence = raw.get("cadence") or {}
    hashtags = raw.get("hashtags") or {}

    return BrandContext(
        slug=raw.get("slug", slug),
        display_name=raw["display_name"],
        tier=raw.get("tier", "brand"),
        policy_profile=raw.get("policy_profile", "standard"),
        slack_channel=normalise_channel(raw.get("slack_channel", "")),
        accounts=tuple(_account(a) for a in raw.get("accounts") or ()),
        voice=_load_voice(directory / "voice.md", slug),
        banned_terms=tuple(raw.get("banned_terms") or ()),
        required_disclaimers=dict(raw.get("required_disclaimers") or {}),
        hashtags=tuple(normalise_hashtag(t) for t in hashtags.get("default") or ()),
        max_per_day=int(cadence.get("max_per_day", 0)),
        max_per_week=int(cadence.get("max_per_week", 0)),
    )


@lru_cache(maxsize=None)
def all_brands() -> tuple[BrandContext, ...]:
    if not BRANDS_DIR.is_dir():
        raise BrandNotFound(f"No brands directory at {BRANDS_DIR}")

    slugs = sorted(
        path.name for path in BRANDS_DIR.iterdir() if (path / "brand.yaml").is_file()
    )
    return tuple(load_brand(slug) for slug in slugs)


def brand_for_channel(channel: str) -> BrandContext:
    """Map a Slack channel to its brand. This is the whole of D3's enforcement.

    Takes a channel NAME -- brand.yaml declares names (`#derekt-socials`), while
    Slack events carry C0... ids. The transport resolves the id once, via
    conversations.info, and passes the name here.
    """
    wanted = normalise_channel(channel)
    for brand in all_brands():
        if brand.slack_channel == wanted:
            return brand
    raise BrandNotFound(f"No brand bound to channel {channel!r}")
