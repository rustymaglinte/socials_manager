"""app.render.post_card -- the post graphic.

Split deliberately. `build_html` is pure and cheap, so the rules worth pinning
down -- escaping, the hook length limit, the palette actually reaching the page
-- are tested without a browser and run in milliseconds. Only one test launches
Chromium, and it asserts the one thing nothing else can: that the pipeline
produces a real PNG of the right size.

The escaping tests are the load-bearing ones. A hook is model-written text going
into a template by string substitution, so an unescaped angle bracket is a
graphic that renders wrong on a live Page rather than an error anyone sees.
"""

import pytest

from app.render.post_card import (
    DEFAULT_SIZE,
    MAX_HOOK_CHARS,
    TEMPLATES,
    PostCard,
    RenderFailed,
    Theme,
    build_html,
)

PINOYSING = Theme(
    ground="#3A3A38",
    accent="#F5D24E",
    muted="#CFCBBD",
    wordmark="PinoySing",
    tagline="online karaoke",
)


def html_for(**overrides) -> str:
    kwargs = {
        "template": "marquee",
        "card": PostCard(hook="8,300+ artists"),
        "theme": PINOYSING,
    }
    return build_html(**{**kwargs, **overrides})


# --- the palette and the template reach the page ------------------------------


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_template_builds(template):
    page = html_for(template=template)

    assert f"t-{template}" in page


def test_an_unknown_template_is_refused():
    with pytest.raises(RenderFailed, match="Unknown template"):
        html_for(template="neon")


def test_the_brands_colours_reach_the_stylesheet():
    page = html_for()

    for colour in (PINOYSING.ground, PINOYSING.accent, PINOYSING.muted):
        assert colour in page


def test_no_placeholder_survives_substitution():
    """A missed token renders as literal __SUB__ on the graphic -- visible, on a
    Page, and only noticed by whoever is looking at it."""
    page = html_for(card=PostCard(hook="Kanta na", sub="Libre", eyebrow="Trivia"))

    for token in ("__SIZE__", "__FONTS__", "__TEMPLATE__", "__GROUND__",
                  "__ACCENT__", "__MUTED__", "__WORDMARK__", "__TAGLINE__",
                  "__HOOK__", "__SUB__", "__EYEBROW__"):
        assert token not in page


def test_the_fonts_are_inlined_rather_than_fetched():
    """A render that reaches out to Google can fall back silently to a default
    face, and publish an off-brand image nobody flagged."""
    page = html_for()

    assert "data:font/woff2;base64," in page
    assert "fonts.googleapis.com" not in page
    assert "fonts.gstatic.com" not in page


def test_the_eyebrow_falls_back_to_the_wordmark():
    page = html_for(card=PostCard(hook="Kanta na"))

    assert PINOYSING.wordmark in page


# --- text handling ------------------------------------------------------------


def test_a_hook_is_escaped_before_it_reaches_the_template():
    """Model-written text goes in by substitution. Unescaped, a stray tag would
    reshape the page it is being put into."""
    page = html_for(card=PostCard(hook="Rock & <b>roll</b>"))

    assert "Rock &amp; &lt;b&gt;roll&lt;/b&gt;" in page
    assert "<b>roll</b>" not in page


def test_the_sub_and_eyebrow_are_escaped_too():
    page = html_for(card=PostCard(hook="Kanta", sub="A & B", eyebrow="<i>x</i>"))

    assert "A &amp; B" in page
    assert "<i>x</i>" not in page


def test_a_newline_in_the_hook_becomes_a_line_break():
    """Where the line breaks is part of the composition, so it is the caller's
    call rather than whatever the box happens to wrap at."""
    page = html_for(card=PostCard(hook="8,300+\nartists"))

    assert "8,300+<br>artists" in page


def test_newlines_break_only_the_hook():
    """The sub is one line by design; letting it break would push the layout off
    the bottom of a template whose spacing assumes a single line."""
    page = html_for(card=PostCard(hook="Kanta", sub="one\ntwo"))

    assert "one<br>two" not in page


def test_an_empty_hook_is_refused():
    with pytest.raises(RenderFailed, match="needs a hook"):
        html_for(card=PostCard(hook="   "))


def test_a_hook_too_long_to_read_is_refused_with_the_reason():
    """Refusing beats shrinking the type until the graphic stops working. The
    message names the fix because the caller has somewhere to put the words."""
    with pytest.raises(RenderFailed, match="caption"):
        html_for(card=PostCard(hook="x" * (MAX_HOOK_CHARS + 1)))


def test_a_hook_at_the_limit_is_allowed():
    page = html_for(card=PostCard(hook="x" * MAX_HOOK_CHARS))

    assert "x" * MAX_HOOK_CHARS in page


# --- scaling ------------------------------------------------------------------


def test_the_size_reaches_the_page_so_one_template_serves_every_resolution():
    page = html_for()
    assert f"{DEFAULT_SIZE}px" in page

    small = build_html(
        template="marquee", card=PostCard(hook="Kanta"), theme=PINOYSING, size=540
    )
    assert "540px" in small


# --- how the browser is launched ----------------------------------------------
#
# Asserted against a stand-in rather than a real Chromium, deliberately. The
# end-to-end test below skips where the browser is not installed, which is
# exactly the condition on a fresh container -- so if the launch arguments were
# only covered there, the one property that decides whether rendering works in
# production would be the one property CI never checks.


class _FakeElement:
    async def screenshot(self, **_kwargs) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"0" * 32


class _FakePage:
    async def set_content(self, *_args, **_kwargs) -> None: ...
    async def evaluate(self, *_args, **_kwargs) -> None: ...
    async def query_selector(self, _selector) -> _FakeElement:
        return _FakeElement()


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def new_page(self, **_kwargs) -> _FakePage:
        return _FakePage()

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, record: dict) -> None:
        self._record = record

    async def launch(self, **kwargs) -> _FakeBrowser:
        self._record.update(kwargs)
        browser = _FakeBrowser()
        self._record["browser"] = browser
        return browser


class _FakePlaywright:
    def __init__(self, record: dict) -> None:
        self.chromium = _FakeChromium(record)

    async def __aenter__(self) -> "_FakePlaywright":
        return self

    async def __aexit__(self, *_exc) -> None: ...


@pytest.fixture
def launched(monkeypatch) -> dict:
    """Render against a stand-in browser; yields how `launch` was called."""
    from app.render import post_card

    record: dict = {}
    monkeypatch.setattr(
        post_card, "async_playwright", lambda: _FakePlaywright(record)
    )
    return record


async def test_chromium_is_launched_with_the_flags_a_container_needs(launched):
    """No sandbox, and no reliance on /dev/shm. Both are deployment facts.

    A container runs as root without user namespaces, and Chromium's setuid
    sandbox refuses to start there -- which surfaces as a RenderFailed on every
    post for any brand that declares a theme, not as anything naming a sandbox.
    `--disable-dev-shm-usage` is the second half: /dev/shm defaults to 64MB in a
    container, and Chromium treats exhausting it as a crashed tab.

    Pinned as a test rather than left as a line of code because the failure it
    prevents cannot be reproduced on the machine this is written on: a developer
    laptop has both a working sandbox and a real /dev/shm.
    """
    from app.render.post_card import render

    await render(template="marquee", card=PostCard(hook="Kanta"), theme=PINOYSING)

    args = launched.get("args", [])
    assert "--no-sandbox" in args
    assert "--disable-dev-shm-usage" in args


async def test_the_browser_is_closed_even_though_the_launch_changed(launched):
    """The flags are new; the guarantee they sit beside is not.

    `render` closes the browser in a `finally`, and a launch argument added
    carelessly is exactly the edit that moves the call out from under it.
    """
    from app.render.post_card import render

    await render(template="marquee", card=PostCard(hook="Kanta"), theme=PINOYSING)

    assert launched["browser"].closed


# --- the one test that needs a browser ----------------------------------------


async def test_rendering_produces_a_square_png():
    """The end-to-end check: a real Chromium, a real screenshot, real bytes.

    Skipped rather than failed where Chromium is not installed -- a fresh clone
    has playwright the package but not the browser until `playwright install`.
    """
    import struct

    from app.render.post_card import render

    size = 240  # small: this is about the pipeline working, not the design
    try:
        png = await render(
            template="stage",
            card=PostCard(hook="23,200+\nkanta", sub="Libre."),
            theme=PINOYSING,
            size=size,
        )
    except RenderFailed as error:
        if "playwright install" in str(error):
            pytest.skip("Chromium not installed; run: playwright install chromium")
        raise

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # IHDR width and height are big-endian uint32 at a fixed offset.
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (size, size)


# --- what a brand.yaml may put into the stylesheet ----------------------------
#
# Colours are substituted straight into CSS while the wordmark, tagline and hook
# beside them are escaped. brand.yaml is trusted config, so this is not
# exploitable today -- it is flagged because the asymmetry is the kind that gets
# copied. The palette is the field most likely to become settable from somewhere
# less trusted (a brand admin page, a theme picker), and the escaping is cheaper
# to add now than to remember later.


@pytest.mark.parametrize("field", ["ground", "accent", "muted"])
def test_a_colour_cannot_close_its_own_declaration(field):
    """A `;` or `}` in a colour would end the rule and start a new one.

    Refused rather than stripped, and the difference matters: sanitising a
    broken colour leaves a *different* colour, so the post renders in the wrong
    shade and nobody finds out until it is on the Page. A render that fails
    naming the field is the better outcome -- FR-7's argument, applied to the
    palette instead of the copy.
    """
    theme = Theme(
        **{
            **{
                "ground": "#111",
                "accent": "#222",
                "muted": "#333",
                "wordmark": "W",
                "tagline": "t",
            },
            field: "red; } body { display: none } .x {",
        }
    )

    with pytest.raises(RenderFailed, match=field):
        build_html(template="marquee", card=PostCard(hook="Kanta"), theme=theme)


@pytest.mark.parametrize(
    "colour", ["#3A3A38", "#F5D24E", "rgb(58, 58, 56)", "hsl(45 88% 63%)"]
)
def test_the_ways_a_palette_is_actually_written_still_work(colour):
    """Sanitising must not cost the brand its own colours.

    brand.yaml uses hex today, but `rgb()` and `hsl()` are ordinary CSS and a
    rule that rejected them would be a worse bug than the one being fixed.
    """
    theme = Theme(
        ground=colour, accent=colour, muted=colour, wordmark="W", tagline="t"
    )

    page = build_html(template="marquee", card=PostCard(hook="Kanta"), theme=theme)

    assert colour in page
