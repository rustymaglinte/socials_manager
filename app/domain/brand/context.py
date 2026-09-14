"""What a brand *is* -- the shapes, and the rules for a valid value.

No I/O and no yaml import: this module has to be readable on its own to answer
"what does the rest of the app get when it asks for a brand?". Reading it off
disk is loader.py's problem.

Framework-free by contract: stdlib only here (SPECS D1).
"""

from dataclasses import dataclass, field
from datetime import time

# Every brand file ships as a template full of TODOs. A TODO rendered into a
# prompt is worse than nothing, so it is detected rather than trusted.
PLACEHOLDER = "TODO"

# Minutes in a day. A slot schedule is clamped to this rather than wrapping:
# see `BrandContext.post_slots`.
_DAY_MINUTES = 24 * 60

# A brand with no timezone declared. Only matters for what "today" means in a
# brief, and UTC is the least surprising thing to be wrong by.
DEFAULT_TIMEZONE = "UTC"


# How each platform's account id is named in the environment, as
# `<PREFIX>_<SLUG>`. Beside `app.credentials.tokens._TOKEN_PREFIX` in spirit --
# FB_PAGE_ID_PINOYSING next to FB_PAGE_TOKEN_PINOYSING -- but defined here,
# because the loader needs the id to decide whether an account is publishable
# and the domain cannot import the credentials layer.
#
# Ids are not in brand.yaml: which Page a brand posts to is deployment state
# (the live Page on Railway, the test Page on a laptop), not brand identity.
ACCOUNT_ID_PREFIX = {
    "facebook": "FB_PAGE_ID",
    "linkedin": "LINKEDIN_ID",
    "x": "X_HANDLE",
    "youtube": "YOUTUBE_CHANNEL_ID",
}


def account_id_env_var(slug: str, platform: str) -> str | None:
    """`pinoysing`, `facebook` -> `FB_PAGE_ID_PINOYSING`; None for an unknown platform."""
    prefix = ACCOUNT_ID_PREFIX.get(platform)
    return f"{prefix}_{slug.upper()}" if prefix else None


class BrandNotFound(LookupError):
    """No such brand directory, or no brand bound to that channel."""


class BrandMisconfigured(ValueError):
    """brand.yaml parsed, but something in it cannot be honoured."""


def normalise_channel(channel: str) -> str:
    """`#Derekt-Socials` and `derekt-socials` are the same channel."""
    return channel.strip().lstrip("#").lower()


def normalise_hashtag(tag: str) -> str:
    """Add the missing '#'. brand.yaml mixes `#trading` and `tradingbot`, and a
    tag without its hash reads as an ordinary word once it is in the prompt."""
    tag = tag.strip()
    return tag if tag.startswith("#") else f"#{tag}"


@dataclass(frozen=True)
class Account:
    platform: str
    enabled: bool
    external_id: str | None

    @property
    def configured(self) -> bool:
        """False while the environment has no id for it (`account_id_env_var`).

        The TODO check predates ids leaving brand.yaml and is kept: a
        `FB_PAGE_ID_X=TODO` copied from a template is still not a Page.

        Nothing is drafted for such an account either (`app.agent.targets`): a
        post nobody can publish still costs a model call and a reviewer's
        attention before the worker dead-letters it.
        """
        return bool(self.external_id) and self.external_id != PLACEHOLDER


@dataclass(frozen=True)
class BriefCatalog:
    """The angles a run can be briefed from, loaded from briefs.yaml.

    Data rather than code (SPECS D1), for the same reason voice is: adding a
    brand should not mean adding a module. `shared` holds the rules that apply
    whichever angle is picked, so they are written once instead of per angle.
    """

    shared: str = ""
    angles: dict[str, str] = field(default_factory=dict)

    @property
    def angle_names(self) -> tuple[str, ...]:
        """Sorted, so a caller picking one has a stable ordering to pick from."""
        return tuple(sorted(self.angles))


@dataclass(frozen=True)
class PostTheme:
    """How this brand's post graphics look.

    Values only, and deliberately not the renderer's own type: `app.render` sits
    above the domain, so a brand knowing what its posts look like must not mean
    the domain importing a renderer. The caller maps this across, which is the
    same join it already makes for credentials.
    """

    ground: str
    accent: str
    muted: str
    wordmark: str
    tagline: str
    # angle name -> template. `default` covers any angle not named.
    templates: dict[str, str] = field(default_factory=dict)

    def template_for(self, angle: str | None) -> str:
        """Which look an angle gets. Unknown angles fall back rather than fail --
        a new angle in briefs.yaml should produce a plain post, not an error."""
        return self.templates.get(angle or "", self.templates.get("default", "marquee"))


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
    # Defaulted, so the fields above stay positionally stable for existing callers.
    # The audience's timezone, not the host's: a Manila brand posting "ngayong
    # araw" from a UTC box would carry yesterday's date for the first 8 hours.
    timezone: str = DEFAULT_TIMEZONE
    briefs: BriefCatalog = field(default_factory=BriefCatalog)
    # None while the brand declares no theme -- it can still post text.
    theme: PostTheme | None = None
    # When the scheduler drafts. Zero/None means "this brand is driven by hand",
    # which is the state every brand is in until brand.yaml says otherwise --
    # so adding a cron to one brand cannot start one for the others.
    every_hours: int = 0
    first_slot: time | None = None
    # Ceiling on how long a scheduled draft waits for a reviewer. Zero means the
    # gap to the next slot decides. Needed once slots are far apart: a post
    # publishes when it is approved, so a long window is a late post.
    approval_hours: int = 0

    @property
    def post_slots(self) -> tuple[time, ...]:
        """The brand-local clock times a draft is started at, earliest first.

        Derived rather than listed, because the three numbers that produce it
        are already in brand.yaml and a hand-written list would be a fourth
        place for them to disagree: `max_per_day` says how many, `first_slot`
        says when the day opens, `every_hours` says how far apart.

        Clamped at midnight rather than wrapped. A brand asking for eight posts
        two hours apart from 09:00 gets the seven that fit; the eighth would be
        tomorrow's 01:00, which is not what "posts per day" meant, and silently
        moving a post into the small hours is worse than dropping it.
        """
        if not (self.every_hours > 0 and self.first_slot and self.max_per_day > 0):
            return ()

        opens = self.first_slot.hour * 60 + self.first_slot.minute
        step = self.every_hours * 60
        slots = []
        for nth in range(self.max_per_day):
            minutes = opens + nth * step
            if minutes >= _DAY_MINUTES:
                break
            slots.append(time(minutes // 60, minutes % 60))
        return tuple(slots)

    @property
    def enabled_accounts(self) -> tuple[Account, ...]:
        return tuple(account for account in self.accounts if account.enabled)

    @property
    def publishable_accounts(self) -> tuple[Account, ...]:
        """Enabled accounts with a real id behind them.

        `enabled_accounts` is which platforms the brand *wants*; this is which
        of them a post could actually reach. The difference is an id variable
        not set in the environment (see `configured`).
        """
        return tuple(
            account for account in self.enabled_accounts if account.configured
        )
