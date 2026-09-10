"""app.agent.tools.web_search -- the one tool that reaches the open internet.

Mostly a test about *when* the API key is read rather than about searching.
Reading it at import time makes a missing key an ImportError, and an ImportError
is the one failure that cannot be reported usefully: it happens before any of
the machinery that would tell somebody about it exists, and it takes down every
module that imports this one on its way past.

The subprocess below is deliberate. `tests/conftest.py` sets TAVILY_API_KEY
before the first `app.*` import -- it has to, or importing this module would
fail and the whole suite with it -- so the property cannot be checked in this
process at all. A child with a clean environment is the only honest test of it.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _in_a_clean_process(source: str) -> subprocess.CompletedProcess:
    """Run `source` with no TAVILY_API_KEY and no .env to fall back on.

    `cwd` is a directory with nothing in it, because `load_dotenv()` walks up
    from the working directory -- run this from the repo root and the developer's
    own .env supplies the key the test is trying to withhold.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TAVILY_API_KEY"}
    }
    environment["PYTHONPATH"] = str(REPO_ROOT)

    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPO_ROOT.parent,
        timeout=120,
    )


def test_importing_the_tool_without_a_key_is_not_an_error():
    """A missing key must not be an ImportError.

    It takes the module down, and with it `app.llm.factory`, `app.main` and
    anything that reaches them -- so the operator sees a traceback from an
    import chain rather than a sentence naming the variable to set.
    """
    result = _in_a_clean_process(
        """
        import app.agent.tools.web_search as module
        print("IMPORTED", module.web_search.name)
        """
    )
    assert "IMPORTED web_search" in result.stdout, result.stderr


def test_searching_without_a_key_says_which_variable_to_set():
    """Deferred, not discarded: the run still has to fail, and say why.

    The audience is an operator who missed a variable when filling in a
    deployment's environment, so the message names the variable rather than
    describing the library's opinion of it.
    """
    result = _in_a_clean_process(
        """
        import asyncio
        from app.agent.tools.web_search import WebSearchUnavailable, web_search

        try:
            asyncio.run(web_search.ainvoke({"query": "anything"}))
        except WebSearchUnavailable as error:
            print("RAISED", error)
        """
    )
    assert "RAISED" in result.stdout, result.stderr
    assert "TAVILY_API_KEY" in result.stdout


def test_the_client_is_built_once_and_reused(monkeypatch):
    """A search per angle, several angles per run -- not a pool per search.

    Keyed on the key itself rather than cached outright, so rotating the
    variable is picked up rather than requiring a restart. That is the same
    trade `app.credentials.tokens.credentials_for` makes, for the same reason.
    """
    from app.agent.tools import web_search as module

    monkeypatch.setenv("TAVILY_API_KEY", "first-key")
    module._client_for.cache_clear()

    first = module.client()
    assert module.client() is first, "a second search must reuse the pool"

    monkeypatch.setenv("TAVILY_API_KEY", "second-key")
    assert module.client() is not first, "a rotated key must build a new client"

    module._client_for.cache_clear()


def test_a_key_that_is_only_whitespace_counts_as_missing(monkeypatch):
    """A variable set to "" in a deployment dashboard is the common typo."""
    from app.agent.tools.web_search import WebSearchUnavailable, client

    monkeypatch.setenv("TAVILY_API_KEY", "   ")
    with pytest.raises(WebSearchUnavailable):
        client()
