"""app.graphics -- the join between a brand and a renderer that must not know it.

No browser. `render_card`'s own work is a field-for-field mapping and one
template lookup; the pixels are `app.render`'s business and are covered there.
What matters here is that the right palette and the right template arrive,
because getting either wrong is how one brand's words end up on another's
ground -- the exact failure the brand-blind contract exists to prevent.
"""

import pytest

from app.domain.brand.context import PostTheme
from app.graphics import RenderFailed, render_card, supports_graphics
from app.render.post_card import PostCard, Theme
from tests.conftest import make_brand

THEME = PostTheme(
    ground="#3A3A38",
    accent="#F5D24E",
    muted="#CFCBBD",
    wordmark="PinoySing",
    tagline="online karaoke",
    templates={"default": "marquee", "trivia_music": "spotlight"},
)


@pytest.fixture
def drawn(monkeypatch):
    """Capture the arguments that would have reached Chromium."""
    calls: list[dict] = []

    async def fake_render(*, template, card, theme, size=1080):
        calls.append({"template": template, "card": card, "theme": theme})
        return b"\x89PNG" + template.encode()

    monkeypatch.setattr("app.graphics.cards.render", fake_render)
    return calls


def test_a_brand_without_a_palette_has_no_graphic():
    """Nullable on purpose: derekt declares no theme and still posts text."""
    assert supports_graphics(make_brand(theme=None)) is False
    assert supports_graphics(make_brand(theme=THEME)) is True


async def test_a_brand_without_a_palette_is_refused_rather_than_guessed(drawn):
    with pytest.raises(RenderFailed, match="declares no theme"):
        await render_card(make_brand(theme=None), angle="trivia_music", hook="A hook")
    assert drawn == []


async def test_the_palette_crosses_field_for_field(drawn):
    await render_card(make_brand(theme=THEME), angle="trivia_music", hook="A hook")

    assert drawn[0]["theme"] == Theme(
        ground="#3A3A38",
        accent="#F5D24E",
        muted="#CFCBBD",
        wordmark="PinoySing",
        tagline="online karaoke",
    )


async def test_the_angle_picks_the_template_not_the_model(drawn):
    """Draft.angle is stored for exactly this: the look is a consequence of the
    brief, and nothing the model writes can choose it."""
    brand = make_brand(theme=THEME)

    await render_card(brand, angle="trivia_music", hook="A hook")
    assert drawn[-1]["template"] == "spotlight"

    # An angle the theme does not name falls back rather than failing -- a new
    # angle in briefs.yaml should produce a plain post, not an error.
    await render_card(brand, angle="an_angle_nobody_mapped", hook="A hook")
    assert drawn[-1]["template"] == "marquee"

    await render_card(brand, angle=None, hook="A hook")
    assert drawn[-1]["template"] == "marquee"


async def test_the_copy_reaches_the_card_unchanged(drawn):
    await render_card(
        make_brand(theme=THEME), angle=None, hook="Six words", sub="a line under it"
    )
    assert drawn[0]["card"] == PostCard(hook="Six words", sub="a line under it")


async def test_the_card_describes_itself_without_carrying_the_bytes(drawn):
    """The two halves land in different columns: bytes in post_media, copy in
    PostVariant.media. A descriptor holding the image would defeat the split."""
    card = await render_card(
        make_brand(theme=THEME), angle="trivia_music", hook="A hook", sub="a sub"
    )

    assert card.image == b"\x89PNGspotlight"
    descriptor = card.descriptor()
    assert descriptor == {
        "kind": "card",
        "template": "spotlight",
        "hook": "A hook",
        "sub": "a sub",
        "sha256": card.sha256,
        "content_type": "image/png",
    }
    assert card.image not in descriptor.values()


async def test_the_digest_is_of_the_bytes(drawn):
    """What lets a log line establish that the image on the Page is the image in
    the review."""
    import hashlib

    card = await render_card(make_brand(theme=THEME), angle=None, hook="A hook")
    assert card.sha256 == hashlib.sha256(card.image).hexdigest()
