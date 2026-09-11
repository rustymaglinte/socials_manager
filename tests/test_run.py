"""app.main.run -- one brief, start to finish.

`test_gate.py` covers `to_resume_decision` thoroughly, and covers it directly.
What it cannot see is the loop that calls it: `run` is what turns an agent that
suspends at a tool call into a conversation that survives several suspensions,
and it had no test at all.

The three things only this level can check:

- **`while`, not `if`.** An agent can hit the gate more than once in a run --
  once per platform, and again for every rewrite after a rejection. An `if`
  would answer the first interrupt and then return a half-finished run's last
  message as though it were the answer.
- **The same `thread_id` on every invoke.** It is LangGraph's key for the
  conversation, so a second one minted on the resume would leave the interrupt
  unfindable and the run wedged.
- **Nothing is spent before the cheap checks.** A brand with no reachable
  platform must fail before the listener, the database or the model.

Everything with a side effect is stubbed. What is under test is the sequencing.
"""

import pytest

from app import main
from app.agent.targets import NoTargetPlatform
from tests.conftest import make_brand

BRAND = make_brand(slug="derekt")


class FakeAgent:
    """An agent that yields a scripted sequence of results.

    Each entry is either an interrupt (a list of pending tool calls) or a final
    message. Records the config it was invoked with, because the thread_id
    travelling unchanged across a resume is itself one of the properties.
    """

    def __init__(self, script):
        self.script = list(script)
        self.configs = []
        self.resumes = []

    async def ainvoke(self, payload, config):
        self.configs.append(config)
        if not isinstance(payload, dict) or "messages" not in payload:
            self.resumes.append(payload)

        step = self.script.pop(0)
        if isinstance(step, list):
            return {
                "__interrupt__": [
                    type("I", (), {"value": {"action_requests": step}})()
                ]
            }
        return {"messages": [type("M", (), {"content": step})()]}


def call(name="submit_for_approval", **args):
    return {"name": name, "args": {"content": "a draft", **args}}


@pytest.fixture
def wired(monkeypatch):
    """Everything `run` touches that is not the loop itself."""
    decisions = []

    async def fake_start_listener():
        pass

    async def fake_to_resume_decision(run, action, revisions):
        decisions.append((run, action, list(revisions)))
        return {"type": "approve"}

    class FakeDraft:
        id = "draft-1"

    async def fake_open_draft(_session, **kwargs):
        fake_open_draft.kwargs = kwargs
        return FakeDraft()

    async def fake_sync_brands(_session, slugs):
        return []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_transaction():
        yield object()

    monkeypatch.setattr(main, "start_listener", fake_start_listener)
    monkeypatch.setattr(main, "to_resume_decision", fake_to_resume_decision)
    monkeypatch.setattr(main, "open_draft", fake_open_draft)
    monkeypatch.setattr(main, "sync_brands", fake_sync_brands)
    monkeypatch.setattr(main, "transaction", fake_transaction)

    def agent_for(script):
        agent = FakeAgent(script)
        monkeypatch.setattr(main, "agent_creation", lambda _brand: agent)
        return agent

    agent_for.decisions = decisions
    agent_for.opened = fake_open_draft
    return agent_for


# --- the loop -----------------------------------------------------------------


async def test_a_run_with_no_gate_returns_the_agents_answer(wired):
    wired(["nothing to submit"])
    assert await main.run("a brief", BRAND) == "nothing to submit"


async def test_one_gate_hit_is_answered_and_the_run_continues(wired):
    agent = wired([[call()], "submitted one post"])

    assert await main.run("a brief", BRAND) == "submitted one post"
    assert len(wired.decisions) == 1


async def test_the_gate_can_be_hit_more_than_once(wired):
    """`while`, not `if`.

    A rejection with a note sends the draft back, so the agent submits again --
    and an `if` here would leave the second interrupt unanswered and return a
    half-finished run's last message as the result.
    """
    agent = wired([[call()], [call()], [call()], "submitted after two rewrites"])

    answer = await main.run("a brief", BRAND)

    assert answer == "submitted after two rewrites"
    assert len(wired.decisions) == 3


async def test_several_pending_calls_are_answered_one_at_a_time(wired):
    """Sequential, not gathered: each puts a prompt in front of the same
    reviewer, and asking three questions at once is worse than asking three
    times."""
    wired([[call(platform="facebook"), call(platform="x")], "done"])

    await main.run("a brief", BRAND)

    platforms = [action["args"].get("platform") for _run, action, _r in wired.decisions]
    assert platforms == ["facebook", "x"]


async def test_the_revision_memory_is_shared_across_gate_hits(wired, monkeypatch):
    """One budget per run, not one per gate hit.

    `revisions` is what bounds a reviewer who keeps rejecting. A fresh list
    handed to each hit would make MAX_REVISIONS unreachable and restore exactly
    the unbounded rewrite loop it exists to stop -- and it would do so
    invisibly, since every individual call still looks correct.
    """
    wired([[call()], [call()], [call()], "done"])
    seen = []

    async def rejecting(_run, _action, revisions):
        seen.append(len(revisions))
        revisions.append("rejected")
        return {"type": "reject", "message": "again"}

    monkeypatch.setattr(main, "to_resume_decision", rejecting)

    await main.run("a brief", BRAND)

    # Each hit sees what the previous ones wrote, rather than starting at zero.
    assert seen == [0, 1, 2]


async def test_a_second_run_starts_with_a_clean_budget(wired, monkeypatch):
    """Passed in rather than global, so one run's rejections cannot shorten the
    next run's rope."""
    seen = []

    async def rejecting(_run, _action, revisions):
        seen.append(len(revisions))
        revisions.append("rejected")
        return {"type": "reject", "message": "again"}

    monkeypatch.setattr(main, "to_resume_decision", rejecting)

    wired([[call()], "done"])
    await main.run("a brief", BRAND)
    wired([[call()], "done"])
    await main.run("another brief", BRAND)

    assert seen == [0, 0]


# --- what travels with the run ------------------------------------------------


async def test_both_invokes_carry_the_same_thread_id(wired):
    """LangGraph's key for the conversation. A second one on the resume would
    leave the interrupt unfindable and the run wedged."""
    agent = wired([[call()], "done"])

    await main.run("a brief", BRAND)

    threads = {config["configurable"]["thread_id"] for config in agent.configs}
    assert len(agent.configs) == 2
    assert len(threads) == 1


async def test_the_draft_records_the_thread_that_produced_it(wired):
    """The only link back from a durable row to the conversation that wrote it
    -- Slack history is explicitly not readable back (SPECS 7.4)."""
    agent = wired(["done"])

    await main.run("a brief", BRAND, angle="trivia")

    opened = wired.opened.kwargs
    assert opened["thread_id"] == agent.configs[0]["configurable"]["thread_id"]
    assert opened["brand_slug"] == "derekt"
    assert opened["angle"] == "trivia"
    assert opened["concept"] == "a brief"


async def test_the_gate_is_told_the_brand_and_angle_the_run_decided(wired):
    """None of which the model may supply (SPECS D3)."""
    wired([[call()], "done"])

    await main.run("a brief", BRAND, angle="trivia", approval_timeout=5400)

    run, _action, _revisions = wired.decisions[0]
    assert run.brand.slug == "derekt"
    assert run.angle == "trivia"
    assert run.approval_timeout == 5400


# --- what must not be spent ---------------------------------------------------


async def test_a_brand_with_nowhere_to_post_fails_before_anything_costs_money(
    wired, monkeypatch
):
    """Raised first, because everything below it costs something and none of it
    can be undone by finding out later.

    The failure is a line of yaml away from fixed, and a run that reached the
    model would burn a search and a model call to produce a draft with no home.
    """
    nowhere = make_brand(
        slug="derekt",
        accounts=(),
    )
    agent = wired(["should never be reached"])

    started = []
    async def listener():
        started.append(True)

    monkeypatch.setattr(main, "start_listener", listener)

    with pytest.raises(NoTargetPlatform):
        await main.run("a brief", nowhere)

    assert agent.configs == [], "the model must not be invoked"
    assert started == [], "the listener must not be opened"


# --- the entry points ----------------------------------------------------------
#
# `brand_from_argv` was written to turn a mistyped brand into a sentence naming
# the ones that exist, and then nothing called it: `main` reached for
# `load_brand` directly, so the friendly message was dead code and what an
# operator actually got was a raw BrandNotFound traceback.
#
# The pool is the other half. `app.workers.publisher` and `app.scheduler` both
# dispose on the way out; `app.main` did not, so the one entry point a developer
# runs by hand was the one that leaked its connections.


def test_a_mistyped_brand_names_the_ones_that_exist(monkeypatch, real_brands):
    monkeypatch.setattr(main.sys, "argv", ["app.main", "pinoysingg"])

    with pytest.raises(SystemExit) as caught:
        main.brand_from_argv()

    assert "pinoysingg" in str(caught.value)


def test_no_argument_prints_the_usage_and_the_brands(monkeypatch, real_brands):
    monkeypatch.setattr(main.sys, "argv", ["app.main"])

    with pytest.raises(SystemExit) as caught:
        main.brand_from_argv()

    message = str(caught.value)
    assert "Usage" in message
    assert "pinoysing" in message


async def test_a_one_shot_run_disposes_its_connection_pool(wired, monkeypatch):
    """Both workers dispose on the way out; the hand-run entry point did not."""
    wired(["done"])
    disposed = []

    async def fake_dispose():
        disposed.append(True)

    async def fake_stop():
        pass

    async def fake_brief_for(_brand):
        return "a brief", "trivia"

    monkeypatch.setattr(main, "dispose", fake_dispose)
    monkeypatch.setattr(main, "stop_listener", fake_stop)
    monkeypatch.setattr(main, "brief_for", fake_brief_for)

    await main.one_shot(BRAND)

    assert disposed == [True]


async def test_the_pool_is_disposed_even_when_the_run_fails(wired, monkeypatch):
    """A crash is exactly when the process is about to exit, so it is exactly
    when the connections need giving back."""
    disposed = []

    async def fake_dispose():
        disposed.append(True)

    async def fake_stop():
        pass

    async def exploding_brief_for(_brand):
        raise RuntimeError("the database is down")

    monkeypatch.setattr(main, "dispose", fake_dispose)
    monkeypatch.setattr(main, "stop_listener", fake_stop)
    monkeypatch.setattr(main, "brief_for", exploding_brief_for)

    with pytest.raises(RuntimeError):
        await main.one_shot(BRAND)

    assert disposed == [True]
