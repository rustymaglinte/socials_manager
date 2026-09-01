"""Reading brands/<slug>/ off disk.

The only module here that touches the filesystem or yaml. Everything it returns
is defined in context.py, and every field it fills comes from a file an operator
hand-edits -- so absent, empty, and still-a-TODO are the normal cases, not the
exceptional ones. The rule throughout: degrade to "this brand cannot do that
yet" with a warning, and reserve raising for what would silently produce a wrong
post (an unknown timezone, a missing brand.yaml).

Framework-free by contract: stdlib and PyYAML only (SPECS D1).
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from app.domain.brand.context import (
    DEFAULT_TIMEZONE,
    PLACEHOLDER,
    Account,
    BrandContext,
    BrandMisconfigured,
    BrandNotFound,
    BriefCatalog,
    normalise_channel,
    normalise_hashtag,
)

logger = logging.getLogger(__name__)

# brands/ sits beside app/ at the repo root. Module-level so tests can repoint
# it -- and they must patch it HERE, on this module, not on the package facade:
# `app.domain.brand.BRANDS_DIR` is a re-exported copy, and rebinding it would
# leave the functions below still reading the real directory.
BRANDS_DIR = Path(__file__).resolve().parents[3] / "brands"

# Platforms name their identifier differently; the loader only needs to find one.
_ID_KEYS = ("handle", "page_id", "channel_id", "external_id")


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


def _load_briefs(path: Path, slug: str) -> BriefCatalog:
    """Read briefs.yaml, dropping anything still carrying a TODO.

    Absent is fine -- a brand can be briefed by hand. A half-written angle is
    not: it would render its TODO straight into the model's first turn. The
    shared block is checked the same way and for the same reason, since it goes
    into every brief this brand produces.
    """
    if not path.is_file():
        return BriefCatalog()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    angles = {}
    for name, text in (raw.get("angles") or {}).items():
        text = (text or "").strip()
        if not text or PLACEHOLDER in text:
            logger.warning(
                "Brief angle %r for %s is empty or still a TODO template; skipping",
                name,
                slug,
            )
            continue
        angles[name] = text

    shared = (raw.get("shared") or "").strip()
    if PLACEHOLDER in shared:
        logger.warning(
            "Shared brief block for %s is still the TODO template; dropping it",
            slug,
        )
        shared = ""

    return BriefCatalog(shared=shared, angles=angles)


def _timezone(name: str, slug: str) -> str:
    """Validate at load, so a typo surfaces here and not mid-run."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BrandMisconfigured(
            f"Brand {slug!r} declares an unknown timezone {name!r}"
        ) from exc
    return name


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
        timezone=_timezone(raw.get("timezone") or DEFAULT_TIMEZONE, slug),
        briefs=_load_briefs(directory / "briefs.yaml", slug),
    )


@lru_cache(maxsize=None)
def all_brands() -> tuple[BrandContext, ...]:
    if not BRANDS_DIR.is_dir():
        raise BrandNotFound(f"No brands directory at {BRANDS_DIR}")

    slugs = sorted(
        path.name for path in BRANDS_DIR.iterdir() if (path / "brand.yaml").is_file()
    )
    return tuple(load_brand(slug) for slug in slugs)
