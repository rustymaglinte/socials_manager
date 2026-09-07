"""One brand's post card: rendered, hashed, and described.

See the package docstring for why this module exists rather than the renderer
knowing what a brand is.
"""

import hashlib
import logging
from dataclasses import dataclass

from app.domain.brand.context import BrandContext
from app.render.post_card import MAX_HOOK_CHARS, PostCard, RenderFailed, Theme, render

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_HOOK_CHARS",
    "Card",
    "RenderFailed",
    "render_card",
    "supports_graphics",
]


@dataclass(frozen=True)
class Card:
    """A rendered graphic, plus what it was made from.

    The two halves land in different places: `image` in `post_media`, because it
    is the artefact a human approved and the publisher uploads, and everything
    else in `PostVariant.media`, which is the column that exists to record what
    should be attached. Kept together here because they are produced together
    and a descriptor that does not describe these bytes would be worse than none.
    """

    image: bytes
    template: str
    hook: str
    sub: str
    sha256: str
    content_type: str = "image/png"

    def descriptor(self) -> dict:
        """What goes into `PostVariant.media`. Text only -- never the bytes."""
        return {
            "kind": "card",
            "template": self.template,
            "hook": self.hook,
            "sub": self.sub,
            "sha256": self.sha256,
            "content_type": self.content_type,
        }


def supports_graphics(brand: BrandContext) -> bool:
    """Whether this brand can have a card rendered at all.

    A brand declares its palette in brand.yaml or it does not. `derekt` does
    not, and until someone writes one its posts are text -- which the domain
    already allows for: `BrandContext.theme` is nullable precisely so a brand
    "can still post text".
    """
    return brand.theme is not None


async def render_card(
    brand: BrandContext, *, angle: str | None, hook: str, sub: str = ""
) -> Card:
    """Render this brand's graphic for one angle. Raises RenderFailed.

    `RenderFailed` is not swallowed, and that is the point of letting it out: a
    hook over `MAX_HOOK_CHARS` produces a sentence naming the limit, which the
    gate hands back to the model as a tool result to fix (FR-7). Deterministic
    code decides whether the type fits; the model rewrites it. That is D9 --
    the model never judges what will be legible at thumbnail size.
    """
    if brand.theme is None:
        raise RenderFailed(
            f"Brand {brand.slug!r} declares no theme in brand.yaml, so it has no "
            f"post graphic to render. Submit the post as text."
        )

    theme = brand.theme
    template = theme.template_for(angle)

    image = await render(
        template=template,
        card=PostCard(hook=hook, sub=sub),
        # Field for field, and deliberately spelled out rather than unpacked:
        # the two dataclasses agreeing today is not a promise that a colour
        # added to one should silently reach the other.
        theme=Theme(
            ground=theme.ground,
            accent=theme.accent,
            muted=theme.muted,
            wordmark=theme.wordmark,
            tagline=theme.tagline,
        ),
    )

    digest = hashlib.sha256(image).hexdigest()
    logger.info(
        "Rendered %s card for %s (angle %r, %d bytes, sha256 %s)",
        template,
        brand.slug,
        angle,
        len(image),
        digest[:12],
    )
    return Card(
        image=image, template=template, hook=hook, sub=sub, sha256=digest
    )
