"""Turning a brand, an angle and six words into a post graphic.

The join `app.render` is forbidden to make. The renderer takes colours and
strings and never learns whose they are ("The renderer is brand-blind" in
pyproject.toml), so somebody above has to map a brand's `PostTheme` across --
and `PostTheme`'s own docstring says as much: *"The caller maps this across,
which is the same join it already makes for credentials."* This is that caller,
and `app.credentials` is the module it is modelled on.

Two things are decided here and nowhere else:

- **The angle chooses the template.** `theme.template_for(angle)` -- which is
  why `Draft.angle` is stored rather than thrown away once the brief is written.
- **A brand with no theme gets no graphic.** `derekt` declares none today, and
  the answer to that is a text post, not a broken render or a guessed palette.

Import from this package, not from the submodule.
"""

from app.graphics.cards import (
    MAX_HOOK_CHARS,
    Card,
    RenderFailed,
    render_card,
    supports_graphics,
)

__all__ = [
    "MAX_HOOK_CHARS",
    "Card",
    "RenderFailed",
    "render_card",
    "supports_graphics",
]
