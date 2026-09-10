"""app.transports.slack_approval.blocks -- Block Kit builders.

Pure functions, so these are ordinary assertions. Two of them guard against a
silent data loss rather than a rendering glitch: `_preview` must mark what it
truncated, and `settle` must drop the buttons while keeping the draft.
"""

import pytest

from app.transports.slack_approval import blocks


def block_types(message: list[dict]) -> list[str]:
    return [block["type"] for block in message]


def header_of(message: list[dict]) -> str:
    return next(
        block["text"]["text"]
        for block in message
        if block.get("block_id") == blocks.HEADER_BLOCK_ID
    )


# --- what a draft may not do to the channel it is reviewed in -----------------
#
# The draft is model-written text, and the model writes it after reading web
# pages nobody vetted. Everything downstream of the gate is protected by a human
# saying yes -- but the approval message itself goes up *before* anyone has
# looked, so whatever the model wrote is already in Slack, rendered, by the time
# the question "should we post this?" is asked.
#
# Slack mrkdwn is not inert. `<!channel>` is a broadcast to everyone in the
# workspace, and `<url|label>` renders as a link whose visible text need not
# resemble where it goes. Neither is something a draft gets to decide.


def test_a_draft_cannot_broadcast_to_the_channel():
    """`<!channel>` in a caption must reach the reviewer as characters.

    The failure this prevents is not a wrong post -- it is the review itself
    paging the whole workspace at 09:00, on a draft that may well be rejected.
    """
    message = blocks.approval_message(
        "req-1", "pinoysing", "facebook", "Kumusta <!channel> everyone"
    )
    preview = message[1]["text"]["text"]

    assert "<!channel>" not in preview
    assert "&lt;!channel&gt;" in preview


def test_a_draft_cannot_disguise_a_link():
    """`<url|label>` would let the draft show one destination and go to another."""
    preview = blocks.approval_message(
        "req-1", "derekt", "facebook", "See <https://evil.example|our docs>"
    )[1]["text"]["text"]

    assert "<https://evil.example|our docs>" not in preview
    assert "&lt;https://evil.example|our docs&gt;" in preview


def test_ampersands_are_escaped_before_the_angle_brackets():
    """Order matters: escaping `<` first would turn `&lt;` into `&amp;lt;`."""
    preview = blocks.approval_message("req-1", "b", "p", "Tom & Jerry <b>")[1][
        "text"
    ]["text"]

    assert preview == "Tom &amp; Jerry &lt;b&gt;"


def test_ordinary_text_is_untouched():
    """Escaping must not become a thing reviewers have to read around."""
    caption = "Kanta tayo! 23,200+ songs. Libre pa rin."
    preview = blocks.approval_message("req-1", "b", "p", caption)[1]["text"]["text"]

    assert preview == caption


def test_the_verdict_line_still_renders_its_mention():
    """Only the draft is escaped -- the header is ours, and `<@U1>` is the point.

    Escaping everything would turn "Approved by @rusty" into a literal
    `<@U123>`, which is a worse message than the one this protects against.
    """
    settled = blocks.settled_message("b", "p", "draft", ":x: Rejected by <@U123>")

    assert "<@U123>" in header_of(settled)


def test_escaping_cannot_push_a_draft_past_slacks_block_limit():
    """A caption of ampersands quintuples in length when escaped.

    Truncating first and escaping afterwards would send a 14,000-character
    block, which Slack rejects outright -- so the approval message for a
    perfectly ordinary post would simply never appear.
    """
    preview = blocks.approval_message(
        "req-1", "b", "p", "&" * blocks.PREVIEW_MAX_CHARS
    )[1]["text"]["text"]

    assert len(preview) <= 3000


@pytest.mark.parametrize(
    ("caption", "leading"),
    [
        # 2800 ampersands escape to exactly 560 whole `&amp;` -- the cut lands
        # on a boundary and the trim below is never needed.
        ("&" * blocks.PREVIEW_MAX_CHARS, ""),
        # One character of offset is all it takes to land the cut inside an
        # entity instead of between two. This is the case that needs the trim,
        # and the one a tidier fixture silently skips.
        ("x" + "&" * blocks.PREVIEW_MAX_CHARS, "x"),
    ],
)
def test_truncation_never_splits_an_escaped_entity(caption, leading):
    """A cut inside `&amp;` leaves `&am`, which renders as those characters.

    Both alignments, because the aligned one exercises none of the logic: it
    passes whether or not the trim exists.
    """
    preview = blocks.approval_message("req-1", "b", "p", caption)[1]["text"]["text"]

    body = preview.removesuffix("\n\n_(truncated)_")
    assert not body.endswith(("&", "&a", "&am", "&amp"))
    assert body.removeprefix(leading).replace("&amp;", "") == ""


def test_approval_message_has_a_header_a_draft_a_footer_and_the_buttons():
    message = blocks.approval_message("req-1", "derekt", "linkedin", "the draft")

    assert block_types(message) == ["section", "section", "context", "actions"]
    assert "*Approval needed*" in header_of(message)
    assert "`linkedin`" in header_of(message)
    assert "*derekt*" in header_of(message)
    assert message[1]["text"]["text"] == "the draft"


def test_every_button_carries_the_request_id_through_the_click():
    """The click payload is the only place the id can come back from."""
    message = blocks.approval_message("req-1", "derekt", "linkedin", "draft")
    buttons = message[-1]["elements"]

    assert [button["action_id"] for button in buttons] == [
        "approve_request",
        "edit_request",
        "reject_request",
    ]
    assert {button["value"] for button in buttons} == {"req-1"}


def test_the_footer_states_the_brand_platform_and_length():
    message = blocks.approval_message("req-1", "derekt", "linkedin", "x" * 1234)

    assert message[2]["elements"][0]["text"] == "`derekt` · `linkedin` · 1,234 characters"


def test_a_draft_that_fits_is_shown_whole():
    content = "x" * blocks.PREVIEW_MAX_CHARS
    message = blocks.approval_message("req-1", "derekt", "linkedin", content)

    assert message[1]["text"]["text"] == content


def test_an_oversized_draft_is_truncated_and_says_so():
    """Silent truncation would let a reviewer approve a post they never read."""
    content = "x" * (blocks.PREVIEW_MAX_CHARS + 500)
    message = blocks.approval_message("req-1", "derekt", "linkedin", content)
    preview = message[1]["text"]["text"]

    assert preview.startswith("x" * blocks.PREVIEW_MAX_CHARS)
    assert preview.endswith("_(truncated)_")
    assert len(preview) < len(content)
    # The footer still reports the real length, not the shortened one.
    assert "3,300 characters" in message[2]["elements"][0]["text"]


def test_settle_removes_the_buttons_and_swaps_the_header():
    live = blocks.approval_message("req-1", "derekt", "linkedin", "the draft")

    settled = blocks.settle(live, ":white_check_mark: Approved by <@U123>")

    assert "actions" not in block_types(settled)
    assert header_of(settled) == ":white_check_mark: Approved by <@U123>"
    assert settled[1]["text"]["text"] == "the draft"  # the record survives
    assert block_types(settled) == ["section", "section", "context"]


def test_settle_leaves_a_message_it_does_not_recognise_alone():
    existing = [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

    assert blocks.settle(existing, "verdict") == existing


def test_settled_message_rebuilds_the_record_when_there_is_no_message_to_edit():
    """Modal submissions and timeouts arrive without the original message."""
    settled = blocks.settled_message("derekt", "x", "the draft", ":x: Rejected")

    assert block_types(settled) == ["section", "section", "context"]
    assert header_of(settled) == ":x: Rejected"
    assert settled[1]["text"]["text"] == "the draft"
    assert "`derekt` · `x`" in settled[2]["elements"][0]["text"]


def test_plain_is_a_single_mrkdwn_section():
    assert blocks.plain("hello") == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}
    ]


def test_notice_modal_offers_no_way_to_submit():
    modal = blocks.notice_modal("Expired", "Already decided.")

    assert modal["type"] == "modal"
    assert "submit" not in modal
    assert modal["title"]["text"] == "Expired"
    assert modal["blocks"][0]["text"]["text"] == "Already decided."


def test_reject_modal_makes_the_reason_optional():
    modal = blocks.reject_modal('{"request_id": "req-1"}')

    assert modal["callback_id"] == "reject_submit"
    assert modal["private_metadata"] == '{"request_id": "req-1"}'
    reason = modal["blocks"][0]
    assert reason["block_id"] == "reason"
    assert reason["optional"] is True
    assert reason["element"]["action_id"] == "value"


def test_edit_modal_opens_on_the_current_draft():
    modal = blocks.edit_modal("derekt · linkedin", "the draft", '{"request_id": "req-1"}')

    assert modal["callback_id"] == "edit_submit"
    assert modal["private_metadata"] == '{"request_id": "req-1"}'
    draft = modal["blocks"][0]
    assert draft["block_id"] == "draft"
    assert draft["label"]["text"] == "derekt · linkedin"
    assert draft["element"]["action_id"] == "content"
    assert draft["element"]["initial_value"] == "the draft"
    assert draft["element"]["multiline"] is True
    assert draft["element"]["max_length"] == blocks.MODAL_MAX_CHARS


def test_edit_modal_label_stays_within_slacks_limit():
    modal = blocks.edit_modal("b" * 300, "draft", "{}")

    assert len(modal["blocks"][0]["label"]["text"]) == 150


# --- the card-only post, which has no caption to show ----------------------
#
# Slack rejects a section block of zero characters outright ("must be more than
# 0 characters"), and it rejects the whole chat.postMessage call rather than
# that one block -- so an empty caption used to kill the run at the gate, after
# the card had already been rendered. An empty caption is legal here: the
# publisher refuses only a post with neither words nor image.


def test_a_post_with_no_caption_still_renders_a_valid_block():
    message = blocks.approval_message("req-1", "pinoysing", "facebook", "")

    assert message[1]["text"]["text"] == blocks.NO_CAPTION
    assert len(message[1]["text"]["text"]) > 0


def test_a_whitespace_only_caption_counts_as_none():
    """Slack measures the string, not its content -- " " passes its length check
    and then renders as a blank gap the reviewer cannot interpret."""
    message = blocks.approval_message("req-1", "pinoysing", "facebook", "   \n  ")

    assert message[1]["text"]["text"] == blocks.NO_CAPTION


def test_the_settled_record_of_a_card_only_post_is_also_valid():
    """Same builder, different caller: a modal submission rebuilds the message
    from scratch, and would fail identically."""
    settled = blocks.settled_message("pinoysing", "facebook", "", ":x: Rejected")

    assert settled[1]["text"]["text"] == blocks.NO_CAPTION


def test_edit_modal_omits_initial_value_rather_than_sending_an_empty_one():
    """Slack validates "" as a missing value, so prefilling an absent caption
    would reject the modal the reviewer is trying to open."""
    modal = blocks.edit_modal("pinoysing · facebook", "", "{}")

    assert "initial_value" not in modal["blocks"][0]["element"]
