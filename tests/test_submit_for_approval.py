"""The gated tool: what the model is asked for, and what it is told back.

No database and no Slack. Two things are worth pinning here, and both are the
kind of breakage that is silent rather than loud:

1. **`runtime` must not reach the model.** It is injected by LangGraph, and if
   that injection ever stopped being recognised the parameter would appear in
   the tool's schema and the model would be asked to invent a ToolRuntime. So
   the tool is driven through a real ToolNode rather than called directly --
   `.ainvoke` on the tool itself does not inject anything, which is exactly why
   calling it that way would prove nothing.

2. **The sentence is a read, not a claim.** By the time this body runs a human
   has approved the post and `app.main`'s gate has committed the rows; what goes
   into the transcript is whatever `scheduled_for_thread` finds, including when
   it finds nothing.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, ClassVar, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode

from app.agent.tools import submit_for_approval as tool_module
from app.agent.tools.submit_for_approval import submit_for_approval

THREAD = "derekt-0c2f"


class _Queued:
    """Enough of a ScheduledPost for the sentence to be composed from."""

    def __init__(self):
        self.id = uuid.uuid4()
        self.scheduled_for = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


class _NullTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def store(monkeypatch):
    """Stand in for the store. `store.found` is what the lookup returns."""

    class Fake:
        found = None
        asked: ClassVar[list[tuple]] = []

    async def lookup(session, *, thread_id, platform):
        Fake.asked.append((thread_id, platform))
        if isinstance(Fake.found, Exception):
            raise Fake.found
        return Fake.found

    monkeypatch.setattr(tool_module, "scheduled_for_thread", lookup)
    monkeypatch.setattr(tool_module, "transaction", lambda: _NullTransaction())
    return Fake


async def call(platform: str = "facebook", thread_id: str | None = THREAD) -> str:
    """Run the tool the way the agent does: through a ToolNode, inside a graph.

    A graph rather than a bare ToolNode because the node reads its runtime from
    the surrounding execution -- outside one it has nothing to inject.
    """

    class State(TypedDict):
        messages: Annotated[list, add_messages]

    graph = StateGraph(State)
    graph.add_node("tools", ToolNode([submit_for_approval]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)

    configurable = {"thread_id": thread_id} if thread_id else {}
    result = await graph.compile().ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_for_approval",
                            "args": {"content": "a post", "platform": platform},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": configurable},
    )
    return result["messages"][-1].content


def test_the_model_is_asked_for_the_post_and_nothing_else():
    """`runtime` is plumbing. A model that saw it would try to fill it in.

    `hook` and `sub` are the card copy and genuinely are the model's to write --
    the palette, the layout and the template are not, and none of them appear
    here: the brand's theme and the brief's angle decide those (D9).
    """
    assert set(submit_for_approval.tool_call_schema.model_fields) == {
        "content",
        "platform",
        "hook",
        "sub",
    }


async def test_the_run_s_thread_is_what_the_tool_looks_up_by(store):
    """The link back to the draft. The model never supplies it -- it comes from
    the config `app.main` minted the run with, which is the same string written
    to Draft.thread_id."""
    store.found = _Queued()
    await call(platform="linkedin")
    assert store.asked == [(THREAD, "linkedin")]


async def test_a_queued_post_is_reported_with_when_it_goes_out(store):
    store.found = _Queued()
    answer = await call()
    assert "queued to publish at 2026-09-07 14:30" in answer
    assert "nothing further to do" in answer


async def test_nothing_queued_is_reported_as_nothing_queued(store):
    """The sentence this replaced said "queued to publish" whether or not
    anything had been. Telling the model the truth is the point of the read."""
    store.found = None
    answer = await call()
    assert "no scheduled post was found" in answer
    assert "queued to publish" not in answer


async def test_a_store_that_is_down_does_not_undo_an_approval(store):
    """A human already said yes and the gate already committed. Failing to read
    that back must not crash the run -- the post publishes either way."""
    store.found = RuntimeError("connection refused")
    answer = await call()
    assert "no scheduled post was found" in answer


async def test_a_run_with_no_thread_still_answers(store):
    """Defensive: nothing in this app invokes the agent without a thread_id, but
    a lookup keyed on None would match every draft that never had one."""
    answer = await call(thread_id=None)
    assert store.asked == []
    assert "no scheduled post was found" in answer
