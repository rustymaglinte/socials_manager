"""Compose the system prompt from segments.

Provider-neutral on purpose. A segment carries `cache_after`, a plain boolean;
translating that into what a route actually wants -- `cache_control` blocks for
Anthropic-via-OpenRouter, nothing at all for a family that caches on its own --
belongs to app/llm/middleware.py. Writing a wire-format marker here would put a
provider detail in app.agent and break the "Provider SDKs are confined to
app.llm" contract.

Order in the cached prefix is: tool definitions, then these segments, then the
conversation. Tools are frozen at about ten (SPECS D8) and owned by the agent
factory; this module assumes only that they come first.
"""

import re
from dataclasses import dataclass

from app.agent.prompts.system_prompt import BRAND_BLOCK, SYSTEM_PROMPT
from app.domain.brand import BrandContext

_NO_VOICE = (
    "No voice profile has been written for this brand yet. Draft in plain, "
    "neutral prose rather than inventing a persona."
)


@dataclass(frozen=True)
class Segment:
    """One piece of the system prompt, and whether a cache boundary follows it."""

    text: str
    cache_after: bool = False


def build_system_prompt(brand: BrandContext) -> list[Segment]:
    """Two segments, two breakpoints.

    Split because their reuse horizons differ: the prefix is identical for every
    brand and every run, the brand block only for every run of one brand. Two
    boundaries mean switching channels invalidates the second and leaves the
    first warm; one boundary would throw the prefix away on every switch.
    """
    return [
        Segment(SYSTEM_PROMPT.strip(), cache_after=True),
        Segment(_brand_block(brand), cache_after=True),
    ]


def render(brand: BrandContext) -> str:
    """The whole prompt as one string, for a route with no caching mechanism."""
    return "\n\n".join(segment.text for segment in build_system_prompt(brand))


def _brand_block(brand: BrandContext) -> str:
    filled = BRAND_BLOCK.strip().format(
        display_name=brand.display_name,
        voice=brand.voice or _NO_VOICE,
        accounts=_accounts(brand),
        banned_terms=_banned_terms(brand.banned_terms),
        disclaimers=_disclaimers(brand.required_disclaimers),
        hashtags=_hashtags(brand.hashtags),
        max_per_day=brand.max_per_day,
        max_per_week=brand.max_per_week,
    )
    # An empty optional section leaves a hole; close it rather than shipping
    # three blank lines to the model.
    return re.sub(r"\n{3,}", "\n\n", filled)


def _accounts(brand: BrandContext) -> str:
    """Only platforms, only enabled ones.

    This is what makes SPECS 2.1 real: the personal brand has no Facebook row,
    so the model never sees Facebook as an option.
    """
    enabled = brand.enabled_accounts
    if not enabled:
        return "None enabled. Do not draft for any platform."
    return "\n".join(f"- {account.platform}" for account in enabled)


def _hashtags(tags: tuple[str, ...]) -> str:
    """A pool to draw from, not a list to paste.

    The platform section caps X and Facebook at 0-2 tags while Derekt declares
    four defaults, so the brand block must not read as "use all of these."
    """
    if not tags:
        return "This brand has no default hashtags. Do not add any."
    return (
        "Draw hashtags from this set, staying within the platform's limit above. "
        "Fewer than the full set is fine; inventing one that is not here is not.\n"
        + ", ".join(tags)
    )


def _banned_terms(terms: tuple[str, ...]) -> str:
    if not terms:
        return "- (none for this brand)"
    return "\n".join(f'- "{term}"' for term in terms)


def _disclaimers(required: dict[str, str]) -> str:
    if not required:
        return ""
    # Sorted for byte-stability: the prefix only caches if it renders identically.
    return "\n".join(
        ["Include verbatim where the context applies:"]
        + [f"- {context}: {text}" for context, text in sorted(required.items())]
    )
