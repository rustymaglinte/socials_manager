"""Block Kit builders.

Pure functions -- no Slack calls, no shared state. Everything here can be
checked by reading it or by printing the output into Block Kit Builder.
"""

MODAL_MAX_CHARS = 3000  # Slack's cap on a plain_text_input value
PREVIEW_MAX_CHARS = 2800  # a section block tops out at 3000

HEADER_BLOCK_ID = "header"  # the line swapped for the verdict once decided


# What stands in for a caption that is deliberately empty. Slack rejects a
# section block whose text is zero characters ("must be more than 0 characters"),
# and an empty caption is a legal post here rather than a mistake: a card can
# carry the whole thing, which is why the publisher refuses only a post with
# neither words nor image, and why `publish_photo` checks the image for
# emptiness instead of the message. So the absence is rendered rather than
# passed through -- a reviewer approving a card-only post should see that there
# is no caption on purpose, not an approval message with a blank where the post
# should be.
NO_CAPTION = "_(no caption — the graphic carries this post)_"


def escape(text: str) -> str:
    """The three characters Slack mrkdwn reads as syntax rather than as text.

    `&` first, or the replacements eat each other: escaping `<` before `&`
    turns the `&` of `&lt;` into `&amp;`, and the reviewer reads `&amp;lt;`.

    Applied to the draft, never to the verdict line above it. The draft is
    model-written text produced after reading web pages nobody vetted; the
    verdict is ours, and its `<@U123>` is a mention we intend to render.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _preview(content: str) -> str:
    """The draft as the reviewer sees it: escaped first, then trimmed to fit.

    That order is the one that works. Slack's cap is on the string it is sent,
    and escaping can quintuple a length -- a caption of ampersands is five
    times its own size once escaped -- so trimming first would build a block
    over the limit and Slack would refuse the whole message. Which is to say
    the approval request for an ordinary post would simply never appear.
    """
    if not content.strip():
        return NO_CAPTION

    escaped = escape(content)
    if len(escaped) <= PREVIEW_MAX_CHARS:
        return escaped

    cut = escaped[:PREVIEW_MAX_CHARS]
    # A cut landing inside `&amp;` leaves `&am`, which renders as those three
    # characters. Back up to the start of the entity it split.
    opened = cut.rfind("&")
    if opened > cut.rfind(";"):
        cut = cut[:opened]
    return cut + "\n\n_(truncated)_"


def plain(text: str) -> list[dict]:
    """A one-section message."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _header(text: str) -> dict:
    return {
        "type": "section",
        "block_id": HEADER_BLOCK_ID,
        "text": {"type": "mrkdwn", "text": text},
    }


def _footer(brand: str, platform: str, content: str) -> dict:
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"`{brand}` · `{platform}` · {len(content):,} characters",
            }
        ],
    }


def approval_message(
    request_id: str, brand: str, platform: str, content: str
) -> list[dict]:
    return [
        _header(f"*Approval needed* — `{platform}` post for *{brand}*"),
        {"type": "section", "text": {"type": "mrkdwn", "text": _preview(content)}},
        _footer(brand, platform, content),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_request",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve and post"},
                    "value": request_id,  # carries the request through the click
                },
                {
                    "type": "button",
                    "action_id": "edit_request",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": request_id,
                },
                {
                    "type": "button",
                    "action_id": "reject_request",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": request_id,
                },
            ],
        },
    ]


def settle(existing: list[dict], verdict: str) -> list[dict]:
    """Turn a live approval message into a settled record of it.

    Keeps the draft visible, drops the buttons, and swaps the header for the
    verdict. Built from the message Slack hands back on the click, so it works
    even for a request the registry has already forgotten.
    """
    settled = []
    for block in existing:
        if block.get("type") == "actions":
            continue  # buttons must not survive a decision
        if block.get("block_id") == HEADER_BLOCK_ID:
            settled.append(_header(verdict))
            continue
        settled.append(block)
    return settled


def settled_message(brand: str, platform: str, content: str, verdict: str) -> list[dict]:
    """A settled record built from scratch.

    Used where there is no original message to work from: a modal submission,
    which arrives without one, and a timeout, which has no click at all.
    """
    return [
        _header(verdict),
        {"type": "section", "text": {"type": "mrkdwn", "text": _preview(content)}},
        _footer(brand, platform, content),
    ]


def notice_modal(title: str, text: str) -> dict:
    """A dead-end modal: says something, offers no submit."""
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": plain(text),
    }


def reject_modal(private_metadata: str) -> dict:
    """Ask why. An empty reason ends the run; a reason sends it back for a revision."""
    return {
        "type": "modal",
        "callback_id": "reject_submit",
        "title": {"type": "plain_text", "text": "Reject draft"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": private_metadata,
        "blocks": [
            {
                "type": "input",
                "block_id": "reason",
                "optional": True,
                "label": {"type": "plain_text", "text": "What needs to change?"},
                "hint": {
                    "type": "plain_text",
                    "text": "Leave blank to stop instead of asking for a rewrite.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "max_length": MODAL_MAX_CHARS,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. the hook is generic; lead with the data point",
                    },
                },
            }
        ],
    }


def edit_modal(label: str, content: str, private_metadata: str) -> dict:
    """`label` names the draft above the box -- "derekt · linkedin"."""
    element = {
        "type": "plain_text_input",
        "action_id": "content",
        "multiline": True,
        "max_length": MODAL_MAX_CHARS,
    }
    # Omitted rather than set to "", for the same reason `_preview` substitutes:
    # Slack validates an empty string as a missing value, so prefilling a
    # card-only draft's absent caption would reject the modal the reviewer is
    # trying to open. Absent, the box simply starts empty, which is correct --
    # there is no caption yet, and typing one is exactly what Edit is for.
    if content:
        element["initial_value"] = content

    return {
        "type": "modal",
        "callback_id": "edit_submit",
        "title": {"type": "plain_text", "text": "Edit draft"},
        "submit": {"type": "plain_text", "text": "Approve and post"},
        "close": {"type": "plain_text", "text": "Cancel"},
        # The submission is a fresh payload with no memory of the message.
        "private_metadata": private_metadata,
        "blocks": [
            {
                "type": "input",
                "block_id": "draft",
                "label": {"type": "plain_text", "text": label[:150]},
                "element": element,
            }
        ],
    }
