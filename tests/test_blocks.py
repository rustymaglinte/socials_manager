"""app.transports.slack_approval.blocks -- Block Kit builders.

Pure functions, so these are ordinary assertions. Two of them guard against a
silent data loss rather than a rendering glitch: `_preview` must mark what it
truncated, and `settle` must drop the buttons while keeping the draft.
"""

from app.transports.slack_approval import blocks


def block_types(message: list[dict]) -> list[str]:
    return [block["type"] for block in message]


def header_of(message: list[dict]) -> str:
    return next(
        block["text"]["text"]
        for block in message
        if block.get("block_id") == blocks.HEADER_BLOCK_ID
    )


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
