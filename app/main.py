import asyncio
import logging

from dotenv import load_dotenv
from langchain.messages import HumanMessage
from langgraph.types import Command

from app.domain.brand import load_brand
from app.llm.factory import agent_creation
from app.transports.slack_approval.approval import request_approval
from app.transports.slack_approval.client import start_listener, stop_listener

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
load_dotenv()

# The brand this run is bound to. Hard-coded until the Slack transport drives the
# run and resolves it from the channel the brief arrived in (SPECS D3); either
# way it is decided here, never read back out of the model's tool arguments.
BRAND = "pinoysing"

# Both invokes must carry the same thread_id, or the resume can't find the run.
# (thread_id is LangGraph's key for a conversation -- nothing to do with Threads.)
RUN_CONFIG = {"configurable": {"thread_id": "social-run-1"}}

BRIEF = """Mag search online ng mga trivia at fun facts about karaoke or music at gumawa ng isang facebook post. 
Siguruhing sundin ang voice at example patterns ng brand na ito.
Magdagdag din ng naaayon na emojis sa iyong post katulad ng mga examples.
Ilimit up to 130 characters ang iyong post."""

# A rejection with a note sends the draft back for a rewrite. Capped, so a
# reviewer who keeps rejecting can't spin the agent indefinitely.
MAX_REVISIONS = 3


async def to_resume_decision(
    action: dict, submitted: set[str], revisions: list[str]
) -> dict:
    """Ask a human about one pending tool call, in the shape the middleware wants.

    `submitted` and `revisions` are the run's memory across gate hits; they are
    passed in rather than global so a second run in the same process starts clean.
    """
    args = action["args"]

    # Platform is what identifies a variant, and it is optional on the tool, so
    # fall back to its default rather than assuming the model sent one. There is
    # no brand argument to read: BRAND decides the channel (SPECS D3).
    platform = args.get("platform", "default")

    if platform in submitted:
        logging.warning("Refusing second %s post; one was already submitted", platform)
        return {
            "type": "reject",
            "message": (
                f"A {platform} post was already submitted for this brief. One "
                f"brief produces one post per platform. Report what was "
                f"submitted and stop. Do not call submit_for_approval for "
                f"{platform} again."
            ),
        }

    verdict = await request_approval(
        brand=BRAND, platform=platform, content=args["content"]
    )

    if verdict["decision"] == "approved":
        submitted.add(platform)
        return {"type": "approve"}

    if verdict["decision"] == "edited":
        submitted.add(platform)
        # Return the complete replacement args, not a diff.
        return {
            "type": "edit",
            "edited_action": {
                "name": action["name"],
                "args": {**args, "content": verdict["content"]},
            },
        }

    # Supplying a message REPLACES the middleware's built-in "do not retry"
    # instruction, so whether the model tries again is decided entirely here.
    reason = verdict.get("reason")

    if reason and len(revisions) < MAX_REVISIONS:
        revisions.append(reason)
        logging.info("Revision %d requested: %s", len(revisions), reason)
        return {
            "type": "reject",
            "message": (
                f"A human rejected this draft with the note: {reason}\n\n"
                f"Rewrite the post to address that note, then call "
                f"submit_for_approval again with the improved version for the "
                f"same platform."
            ),
        }

    if reason:
        logging.warning("Revision limit (%d) reached; stopping.", MAX_REVISIONS)

    # No reason given, or out of revisions: end the run rather than re-rolling blind.
    return {
        "type": "reject",
        "message": (
            "A human rejected this draft in Slack and nothing was submitted. "
            "The task is over. Report that it was rejected and stop. Do not call "
            "this tool again."
        ),
    }


async def run(brief: str) -> str:
    """One brief, start to finish. Returns the agent's closing message."""
    # Connect before the agent runs, so the socket is live when the first click
    # lands -- and on this loop, so the listener keeps serving while we await a
    # verdict inside the gate.
    logging.info("Connecting Slack listener...")
    await start_listener()

    # One slug decides both the context the model gets and the channel the draft
    # is shown in, so the two can never disagree (SPECS D3).
    agent = agent_creation(load_brand(BRAND))

    # One post per platform. The model will sometimes offer a second variant for
    # a platform it already submitted, and every extra call would cost the
    # reviewer another Slack prompt -- so refuse it here rather than asking.
    submitted: set[str] = set()  # platforms already sent to the reviewer
    revisions: list[str] = []

    logging.info("Sending brief to agent...")
    response = await agent.ainvoke(
        {"messages": [HumanMessage(content=brief)]}, RUN_CONFIG
    )

    # A while, not an if: the agent can hit the gate more than once in a run.
    while "__interrupt__" in response:
        hitl_request = response["__interrupt__"][0].value
        logging.info(
            "Gate hit: %d pending tool call(s) -> %s",
            len(hitl_request["action_requests"]),
            [a["name"] for a in hitl_request["action_requests"]],
        )
        # Sequential, not gathered: each of these puts a prompt in front of the
        # same reviewer, and asking them three questions at once is worse than
        # asking three times.
        decisions = [
            await to_resume_decision(action, submitted, revisions)
            for action in hitl_request["action_requests"]
        ]
        response = await agent.ainvoke(
            Command(resume={"decisions": decisions}), RUN_CONFIG
        )

    return response["messages"][-1].content


async def main() -> None:
    try:
        answer = await run(BRIEF)
        logging.info("Printing agent's response...")
        print(answer)
    finally:
        # The socket is a task on this loop; leaving it open makes asyncio.run()
        # tear down with the connection still live.
        await stop_listener()


if __name__ == "__main__":
    asyncio.run(main())
