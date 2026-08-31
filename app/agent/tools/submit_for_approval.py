import logging

from langchain.tools import tool

logger = logging.getLogger(__name__)


@tool
async def submit_for_approval(content: str, platform: str = "default") -> str:
    """Submit a finished draft to the human reviewer.

    This does NOT publish. It puts the draft in front of a human, who approves,
    edits, or rejects it. Call it once per platform, as soon as that platform's
    draft is ready.
    """
    # Only reached after a human said yes: the middleware interrupts before this
    # body runs (SPECS 8, "the interrupt point"), so arriving here means a
    # verdict already came back from the reviewer.
    #
    # Async ahead of the real adapter: publishing is an HTTP call per platform,
    # and this sits on the same loop as the Slack gate that precedes it.
    logger.info("Reviewer accepted the %s draft", platform)
    return f"Accepted: the {platform} draft cleared review and is queued to publish."
