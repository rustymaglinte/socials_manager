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
from app.domain.brand.context import PLACEHOLDER, Account, PostTheme
from app.render.post_card import MAX_HOOK_CHARS
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
    assert "None" not in block.split("## Platforms this run targets")[0]


def listed_platforms(brand) -> list[str]:
    """The bullets under the target list, and nothing else in that section."""
    block = brand_block(brand)
    section = block.split("## Platforms this run targets")[1].split("## Post graphic")[0]
    return [
        line[2:].strip() for line in section.splitlines() if line.startswith("- ")
    ]


def test_only_reachable_platforms_are_offered():
    """The model never sees a platform it may not target -- that is SPECS 2.1.

    make_brand()'s three accounts fail for three different reasons, and only
    one survives: linkedin is enabled and configured but has no publisher
    adapter, x is disabled, facebook is the target.
    """
    assert listed_platforms(make_brand()) == ["facebook"]


def test_an_account_still_on_its_todo_placeholder_is_not_a_target():
    """Draftable but not publishable (SPECS Q2) -- and drafting for it would
    spend a reviewer's attention on a post the worker then dead-letters."""
    brand = make_brand(
        accounts=(Account(platform="facebook", enabled=True, external_id=PLACEHOLDER),)
    )

    assert listed_platforms(brand) == []


def test_the_target_list_is_stated_as_settled_not_offered_as_a_menu():
    """The bug this is about: the model read the old list as a menu and replied
    'Facebook, X, or YouTube?' -- which ends the run, since only a tool call can
    suspend it and nobody can answer a plain message."""
    block = brand_block(make_brand())

    assert "do not ask which platform to write for" in block


def test_account_identifiers_never_reach_the_prompt():
    """Platforms only. A page id in the prompt is context spent for nothing."""
    block = brand_block(make_brand())

    assert "12345" not in block
    assert "67890" not in block


def test_a_brand_with_nothing_reachable_is_told_not_to_draft():
    """Rendered rather than raised: composing a prompt must not fail. The run
    is what refuses -- see `app.agent.targets.require_targets`."""
    block = brand_block(make_brand(accounts=()))

    assert "None available. Do not draft for any platform." in block


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


def test_a_brand_with_a_card_is_told_to_write_one():
    """Whether there is a card is a property of the brand, so it belongs in the
    brand block -- the same argument as the account list."""
    themed = make_brand(
        theme=PostTheme(
            ground="#3A3A38",
            accent="#F5D24E",
            muted="#CFCBBD",
            wordmark="PinoySing",
            tagline="online karaoke",
        )
    )
    assert "This brand posts a rendered card" in brand_block(themed)


def test_a_brand_with_no_card_is_told_to_leave_the_hook_empty():
    """derekt declares no theme. Offering it a graphic would cost a round trip
    to produce copy that is then discarded."""
    block = brand_block(make_brand(theme=None))

    assert "no post card" in block
    assert "text only" in block


def test_the_hook_limit_the_model_is_told_is_the_one_the_renderer_enforces():
    """A prompt promising a different number would produce a rejection the model
    could not have avoided, on every post."""
    assert f"at most {MAX_HOOK_CHARS} characters" in SYSTEM_PROMPT
