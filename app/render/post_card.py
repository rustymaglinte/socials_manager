"""Rendering a brand's post graphic to PNG bytes.

Screenshots the same HTML/CSS the design was approved in, rather than
re-implementing the layout against a drawing library. The template is then the
single source of what a post looks like: what was signed off is literally what
uploads, and a change to the look is a change to one file.

Brand-blind, like the adapters (SPECS D1): this module takes colours and strings,
never a BrandContext. Which brand owns a palette is the caller's business, and
keeping it out of here is what stops a renderer from being able to put Derekt's
words on PinoySing's ground.

Fonts are bundled beside this module rather than fetched from Google at render
time. A network hiccup mid-render would otherwise fall back to a default face
silently and publish an off-brand image -- a failure nobody notices until it is
on the Page.
"""

import base64
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "templates" / "post.html"
FONTS_DIR = HERE / "fonts"

# Facebook renders feed images at 1:1 for square uploads and the Page grid crops
# to square regardless, so this is the one shape that survives both.
DEFAULT_SIZE = 1080

TEMPLATES = ("marquee", "spotlight", "stage")

# What Chromium needs to start inside a container, and neither flag is optional
# where this actually runs.
#
# `--no-sandbox`: the setuid sandbox needs user namespaces, which a container
# running as root does not have. Without this Chromium refuses to launch at all,
# and it surfaces here as a RenderFailed on every post for every brand that
# declares a theme -- an error about a browser, on a code path whose subject is
# a caption.
#
# `--disable-dev-shm-usage`: /dev/shm defaults to 64MB in a container, and
# Chromium puts shared memory there. Exhausting it is reported as a crashed tab
# rather than as a full filesystem, so it is the second flag every headless
# deployment ends up adding after losing an afternoon to the first.
#
# Harmless on a developer machine, which is the trouble: a laptop has a working
# sandbox and a real /dev/shm, so nothing here can be verified by running it
# locally. tests/test_render.py pins it against a stand-in browser instead.
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

# woff2 rather than ttf: a third of the bytes, and Chromium is the only renderer
# this has to satisfy. Weight is declared per family because Archivo ships as a
# variable font whose single file covers the whole range.
_FONT_FACES = (
    ("Anton", "Anton.woff2", "400"),
    ("Archivo", "Archivo.woff2", "500 700"),
)


class RenderFailed(RuntimeError):
    """The graphic could not be produced, so there is nothing to post."""


@dataclass(frozen=True)
class Theme:
    """A brand's post palette and furniture.

    Two colours and no third by design: `ground` and `accent` swap roles between
    templates, and a third would leave the system with a decision to make on
    every post.
    """

    ground: str  # the dark: charcoal for PinoySing
    accent: str  # the brand colour: yellow for PinoySing
    muted: str  # subtitle grey, legible on `ground`
    wordmark: str  # "PinoySing"
    tagline: str  # "online karaoke"


@dataclass(frozen=True)
class PostCard:
    """What goes on the graphic. Short by construction.

    A square holds roughly six words before it stops being readable on a phone,
    which is a quarter of what the brief allows a caption. The split is the
    caller's to make; this only refuses the clearly-too-long.
    """

    hook: str  # the six words. "\n" forces a line break.
    sub: str = ""  # one supporting line, optional
    eyebrow: str = ""  # marquee/spotlight only; the angle name, or the wordmark


# Past this the type has to shrink far enough that the graphic stops working,
# and silently rendering something unreadable is worse than refusing.
MAX_HOOK_CHARS = 42


# What a CSS value is allowed to contain. Hex, `rgb(...)`, `hsl(...)`, a named
# colour, a `var(--x)` -- letters, digits, and the punctuation those need.
#
# Notably absent: `;` and `}`, either of which ends the declaration it sits in
# and starts something else. brand.yaml is trusted config, so this is not a hole
# anybody can reach today; it is here because the hook, wordmark and tagline
# beside it are all escaped and the palette was not, and that asymmetry is the
# kind that gets copied the day a theme becomes settable from somewhere less
# trusted.
_CSS_VALUE = re.compile(r"^[#\w\s.,()%/+-]*$")


def _colour(value: str, field: str) -> str:
    """A palette entry, refused rather than sanitised if it is not one.

    Refused because there is no safe repair: stripping the punctuation out of a
    broken colour leaves a different colour, and a brand whose posts quietly
    render in the wrong shade is worse off than one whose render fails with the
    name of the field to fix.
    """
    if not _CSS_VALUE.match(value):
        raise RenderFailed(
            f"The theme's {field} is not a usable CSS colour: {value!r}. "
            f"Use a hex code, rgb(), hsl() or a colour name in brand.yaml."
        )
    return value


def _font_css() -> str:
    """@font-face rules with the files inlined.

    Data URIs rather than file:// paths: the page is handed to Chromium as a
    string with no base URL, so a relative href has nothing to resolve against.
    """
    rules = []
    for family, filename, weight in _FONT_FACES:
        path = FONTS_DIR / filename
        if not path.is_file():
            raise RenderFailed(
                f"Missing bundled font {path}. Fonts ship with the repo; "
                f"a fresh clone should already have them."
            )
        encoded = base64.b64encode(path.read_bytes()).decode()
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:normal;"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{encoded}) format('woff2');}}"
        )
    return "\n".join(rules)


def build_html(
    *, template: str, card: PostCard, theme: Theme, size: int = DEFAULT_SIZE
) -> str:
    """The page Chromium will screenshot. Separated from rendering so it can be
    inspected in a browser, and asserted on in a test, without a browser."""
    if template not in TEMPLATES:
        raise RenderFailed(f"Unknown template {template!r}; expected one of {TEMPLATES}")
    if not card.hook.strip():
        raise RenderFailed("A post card needs a hook")
    if len(card.hook) > MAX_HOOK_CHARS:
        raise RenderFailed(
            f"Hook is {len(card.hook)} characters; {MAX_HOOK_CHARS} is the most "
            f"that stays readable at thumbnail size. Move the rest to the caption."
        )

    # Escaped before substitution, not after: the hook is model-written text and
    # an unescaped & or < would otherwise reshape the page it is being put into.
    # Newlines survive as <br> because a deliberate line break is part of the
    # composition, and only the hook gets them.
    hook = html.escape(card.hook).replace("\n", "<br>")

    replacements = {
        "__SIZE__": str(size),
        "__FONTS__": _font_css(),
        "__TEMPLATE__": template,
        # Checked rather than escaped: these land in CSS, where the escaping
        # that protects the text below would be meaningless. See `_colour`.
        "__GROUND__": _colour(theme.ground, "ground"),
        "__ACCENT__": _colour(theme.accent, "accent"),
        "__MUTED__": _colour(theme.muted, "muted"),
        "__WORDMARK__": html.escape(theme.wordmark),
        "__TAGLINE__": html.escape(theme.tagline),
        "__HOOK__": hook,
        "__SUB__": html.escape(card.sub),
        "__EYEBROW__": html.escape(card.eyebrow or theme.wordmark),
    }

    page = TEMPLATE_PATH.read_text(encoding="utf-8")
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page


async def render(
    *, template: str, card: PostCard, theme: Theme, size: int = DEFAULT_SIZE
) -> bytes:
    """PNG bytes for one post graphic.

    Launches a browser per call. That is the wrong shape for a batch and the
    right one for this system: posts are produced one at a time behind a human
    approving each, so a long-lived browser would sit idle holding memory
    between them.
    """
    page_html = build_html(template=template, card=card, theme=theme, size=size)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=LAUNCH_ARGS)
            try:
                page = await browser.new_page(
                    viewport={"width": size, "height": size},
                    # Screenshot pixels should equal CSS pixels; on a HiDPI host
                    # the default would silently produce a 2x image.
                    device_scale_factor=1,
                )
                await page.set_content(page_html, wait_until="load")
                # font-display:block plus this means the screenshot cannot catch
                # a frame where the fallback face is still painted.
                await page.evaluate("document.fonts.ready")
                element = await page.query_selector("#card")
                if element is None:
                    raise RenderFailed("Template produced no #card element")
                png = await element.screenshot(type="png")
            finally:
                await browser.close()
    except RenderFailed:
        raise
    except Exception as error:  # playwright raises broadly; all of it is a failed render
        raise RenderFailed(
            f"Could not render the {template} card: {error}. If this is a fresh "
            f"clone, run: playwright install chromium"
        ) from error

    logger.info("Rendered %s card, %d bytes", template, len(png))
    return png
