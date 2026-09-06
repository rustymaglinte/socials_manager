"""What a brand *is* -- the shapes, and the rules for a valid value.

No I/O and no yaml import: this module has to be readable on its own to answer
"what does the rest of the app get when it asks for a brand?". Reading it off
disk is loader.py's problem.

Framework-free by contract: stdlib only here (SPECS D1).
"""

from dataclasses import dataclass, field

# Every brand file ships as a template full of TODOs. A TODO rendered into a
# prompt is worse than nothing, so it is detected rather than trusted.
PLACEHOLDER = "TODO"

# A brand with no timezone declared. Only matters for what "today" means in a
# brief, and UTC is the least surprising thing to be wrong by.
DEFAULT_TIMEZONE = "UTC"


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
        """False while the yaml still says TODO -- draftable, but not publishable."""
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

    @property
    def enabled_accounts(self) -> tuple[Account, ...]:
        return tuple(account for account in self.accounts if account.enabled)
