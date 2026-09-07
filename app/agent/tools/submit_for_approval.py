import logging

from langchain.tools import ToolRuntime, tool

from app.store.engine import transaction
from app.store.repositories import scheduled_for_thread

logger = logging.getLogger(__name__)


async def _queued(thread_id: str | None, platform: str):
    """The scheduled post this run queued for `platform`, or None.

    Never raises. By the time this runs a human has already approved the post
    and the gate has already committed the rows, so a failure to *read* them
    back must not turn an approval into a crashed run -- the post publishes
    either way. The sentence below degrades instead.
    """
    if not thread_id:
        return None
    try:
        async with transaction() as session:
            return await scheduled_for_thread(
                session, thread_id=thread_id, platform=platform
            )
    except Exception:
        logger.exception("Could not read back the %s schedule for %s", platform, thread_id)
        return None


@tool
async def submit_for_approval(
    content: str,
    runtime: ToolRuntime,
    platform: str = "default",
    hook: str = "",
    sub: str = "",
) -> str:
    """Submit a finished draft to the human reviewer.

    This does NOT publish. It puts the draft in front of a human, who approves,
    edits, or rejects it. Call it once per platform, as soon as that platform's
    draft is ready.

    Args:
        content: the caption -- the full post text.
        platform: which platform this variant is for.
        hook: the words that go ON the graphic, at most 42 characters. Six words
            is about the limit before it stops being readable on a phone. Leave
            it empty for a text-only post. You do not choose the colours, the
            layout or the template -- the brand's theme and the brief's angle
            decide those. You write the words.
        sub: one optional supporting line under the hook, also short.
    """
    # Only reached after a human said yes: the middleware interrupts before this
    # body runs (SPECS 8, "the interrupt point"), so arriving here means a
    # verdict already came back from the reviewer -- and that `app.main`'s gate
    # has already written the approval and the schedule it permits.
    #
    # So there is nothing to do here but report, and the report is a read rather
    # than a claim. The old version of this line told the model the post was
    # "queued to publish" whether or not anything had been queued; what goes
    # into the transcript now is what is in the database.
    #
    # `runtime` is injected by LangGraph, not supplied by the model, and carries
    # the run's config -- which is where the thread_id `Draft.thread_id` was
    # written with lives.
    thread_id = (runtime.config.get("configurable") or {}).get("thread_id")
    queued = await _queued(thread_id, platform)

    if queued is None:
        logger.warning("Reviewer accepted the %s draft but nothing is queued", platform)
        return (
            f"The {platform} draft cleared review, but no scheduled post was "
            f"found for it. Report that it was approved and that publishing "
            f"needs an operator to look; do not resubmit."
        )

    logger.info(
        "Reviewer accepted the %s draft; queued as %s for %s",
        platform,
        queued.id,
        queued.scheduled_for,
    )
    return (
        f"Accepted: the {platform} draft cleared review and is queued to "
        f"publish at {queued.scheduled_for:%Y-%m-%d %H:%M %Z}. The publisher "
        f"sends it; you have nothing further to do for {platform}."
    )
