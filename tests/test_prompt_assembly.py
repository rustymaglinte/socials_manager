"""app.agent.prompts.assembly -- what the model is actually told.

Two properties are worth more than the rest and are checked here explicitly:

- Byte-stability. The prefix only caches if it renders identically each time, so
  anything derived from a dict (disclaimers) has to be ordered.
- Brand isolation (SPECS 2.1). A platform the brand has not enabled, and another
  brand's data, must not appear in the block at all -- the model cannot target
  what it is never shown.
"""

import re

from app.agent.prompts.assembly import Segment, build_system_prompt, render
from app.agent.prompts.system_prompt import SYSTEM_PROMPT
from tests.conftest import make_brand


def brand_block(brand) -> str:
    """The second segment -- everything that varies per brand."""
    return build_system_prompt(brand)[1].text


def test_two_segments_each_ending_a_cache_boundary():
    segments = build_system_prompt(make_brand())

    assert len(segments) == 2
    assert all(isinstance(segment, Segment) for segment in segments)
    assert [segment.cache_after for segment in segments] == [True, True]


def test_the_prefix_is_the_shared_prompt_and_is_identical_across_brands():
    """One boundary would throw this away on every channel switch; two do not."""
    derekt = build_system_prompt(make_brand(slug="derekt"))[0]
    personal = build_system_prompt(
        make_brand(slug="personal", display_name="Rusty", voice="Plain.")
    )[0]

    assert derekt.text == SYSTEM_PROMPT.strip()
    assert derekt.text == personal.text


def test_render_joins_the_segments_in_order():
    brand = make_brand()
    segments = build_system_prompt(brand)

    assert render(brand) == f"{segments[0].text}\n\n{segments[1].text}"


def test_no_format_placeholder_survives_into_the_prompt():
    rendered = render(make_brand())

    assert not re.search(r"\{[a-z_]+\}", rendered)


def test_the_block_names_the_brand_and_carries_its_voice():
    block = brand_block(make_brand(display_name="Derekt", voice="Dry, numbers first."))

    assert "# Brand: Derekt" in block
    assert "Dry, numbers first." in block


def test_a_brand_with_no_voice_gets_the_neutral_instruction_not_an_empty_section():
    block = brand_block(make_brand(voice=None))

    assert "No voice profile has been written" in block
    assert "None" not in block.split("## Accounts")[0]


def test_only_enabled_platforms_are_offered():
    """The model never sees a platform it may not target -- that is SPECS 2.1."""
    block = brand_block(make_brand())
    accounts = block.split("## Accounts you may target")[1].split("## Hard rules")[0]

    assert "- linkedin" in accounts
    assert "- facebook" in accounts
    assert "x" not in accounts  # present in brand.yaml, but enabled: false


def test_account_identifiers_never_reach_the_prompt():
    """Platforms only. A page id in the prompt is context spent for nothing."""
    block = brand_block(make_brand())

    assert "12345" not in block
    assert "67890" not in block


def test_a_brand_with_nothing_enabled_is_told_not_to_draft():
    block = brand_block(make_brand(accounts=()))

    assert "None enabled. Do not draft for any platform." in block


def test_hashtags_read_as_a_pool_not_a_checklist():
    block = brand_block(make_brand(hashtags=("#trading", "#quant")))

    assert "#trading, #quant" in block
    assert "Draw hashtags from this set" in block


def test_a_brand_with_no_hashtags_is_told_not_to_invent_any():
    block = brand_block(make_brand(hashtags=()))

    assert "no default hashtags. Do not add any." in block


def test_banned_terms_are_quoted_one_per_line():
    block = brand_block(make_brand(banned_terms=("guaranteed returns", "risk-free")))

    assert '- "guaranteed returns"' in block
    assert '- "risk-free"' in block


def test_a_brand_with_no_banned_terms_says_so():
    block = brand_block(make_brand(banned_terms=()))

    assert "- (none for this brand)" in block


def test_disclaimers_render_verbatim_in_a_stable_order():
    """Sorted, not dict order: an unstable prefix is an uncached prefix."""
    forwards = brand_block(
        make_brand(required_disclaimers={"advice": "Not advice.", "perf": "No guide."})
    )
    backwards = brand_block(
        make_brand(required_disclaimers={"perf": "No guide.", "advice": "Not advice."})
    )

    assert forwards == backwards
    assert "- advice: Not advice." in forwards
    assert forwards.index("- advice:") < forwards.index("- perf:")


def test_an_empty_optional_section_leaves_no_hole():
    block = brand_block(make_brand(required_disclaimers={}))

    assert "\n\n\n" not in block


def test_the_cadence_ceiling_is_stated():
    block = brand_block(make_brand(max_per_day=2, max_per_week=10))

    assert "Cadence ceiling: 2/day, 10/week." in block


def test_one_brands_prompt_contains_nothing_of_the_others():
    derekt = make_brand()
    personal = make_brand(
        slug="personal",
        display_name="Rusty Maglinte",
        voice="Plain and direct.",
        accounts=(derekt.accounts[0],),
        banned_terms=(),
        required_disclaimers={},
        hashtags=(),
    )

    block = brand_block(personal)

    assert "Derekt" not in block
    assert "guaranteed returns" not in block
    assert "#trading" not in block
    assert "facebook" not in block
