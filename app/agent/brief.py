"""Build the brief a run opens with.

Deliberately not in app.agent.prompts: that package composes the cached prefix
(assembly.py is careful about keeping it byte-stable), and a brief is the
conversation turn that follows it -- the HumanMessage, not the system prompt.

The angles themselves are brand data (brands/<slug>/briefs.yaml), so adding a
brand adds nothing here.

Two kinds of repetition to fight, and only one of them lives in this module:
angle repetition, handled by `pick_angle`; and topic repetition, which no amount
of angle-shuffling touches, since every run searches the same web from an empty
context. That one needs what was actually posted, which is why `build_brief`
takes `recent` -- FR-4 and FR-6. It is a parameter rather than a store lookup
because app.store does not exist yet, and because the caller deciding what
counts as "recent" keeps this function pure.
"""

import logging
import random
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain.brand import BrandContext

logger = logging.getLogger(__name__)

# How the date reads inside a brief. English month names, matching what the
# angles were written against.
_DATE_FORMAT = "%B %d, %Y"

_AVOID_HEADER = (
    "Huwag ulitin ang mga paksa ng mga nakaraang post na ito. Pumili ng "
    "ibang anggulo o ibang paksa:"
)


class BriefError(ValueError):
    """The catalog cannot produce a brief -- an unknown angle, or none at all."""


class _Tokens(dict):
    """Turns an unfilled placeholder into a message that names it."""

    def __missing__(self, key: str) -> str:
        raise BriefError(
            f"Brief template uses an unknown placeholder {{{key}}}; "
            f"available: {', '.join(sorted(self))}"
        )


def today(brand: BrandContext) -> str:
    """The brand audience's today, resolved now rather than at import.

    The bug this exists to prevent: an f-string in a module-level dict is
    evaluated once, when the module is first imported. A process that holds a
    Slack socket open for days (main.py) or a publisher worker (FR-12) would
    keep briefing the model with the date it booted on.
    """
    return datetime.now(ZoneInfo(brand.timezone)).strftime(_DATE_FORMAT)


def pick_angle(
    brand: BrandContext,
    recent: Sequence[str] = (),
    rng: random.Random | None = None,
) -> str:
    """Choose an angle the brand has not just used.

    Random over the unused ones, not a fixed rotation: a rotation is predictable
    to anyone reading the feed, and falls apart the moment a run is skipped.
    Falls back to the full set once `recent` has covered everything, so a
    two-angle brand with three recent runs still gets a brief.
    """
    names = brand.briefs.angle_names
    if not names:
        raise BriefError(f"Brand {brand.slug!r} has no brief angles configured")

    unused = tuple(name for name in names if name not in set(recent))
    if not unused:
        logger.info("Every angle for %s is recent; reusing the full set", brand.slug)
        unused = names

    return (rng or random).choice(unused)


def build_brief(
    brand: BrandContext,
    angle: str,
    recent: Sequence[str] = (),
) -> str:
    """One angle plus the brand's shared rules, with what not to repeat.

    `angle` is passed in rather than chosen here so the scheduler owns the
    decision and a test can force one. `recent` is prior post content or topics;
    it is appended after templating, never through it, since a real post can
    contain a brace and is not ours to treat as a format string.
    """
    catalog = brand.briefs
    if angle not in catalog.angles:
        raise BriefError(
            f"Brand {brand.slug!r} has no brief angle {angle!r}; "
            f"available: {', '.join(catalog.angle_names) or '(none)'}"
        )

    tokens = _Tokens(today=today(brand))
    parts = [
        text.format_map(tokens)
        for text in (catalog.angles[angle], catalog.shared)
        if text
    ]

    if recent:
        parts.append(_AVOID_HEADER + "\n" + "\n".join(f"- {item}" for item in recent))

    return "\n\n".join(parts)
