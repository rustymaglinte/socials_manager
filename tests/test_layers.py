"""The one layering rule that was never actually holding.

`lint-imports` is the real enforcement and `pyproject.toml` carries all six
contracts. This file is not a replacement for it -- it pins the single edge that
had been broken since the initial commit, so the regression cannot come back
quietly between runs of a linter that is not installed in the default venv and
is therefore not part of `pytest`.

The rule, from the layer stack in pyproject.toml:

    app.agent | app.workers | app.graphics      <- higher
    app.llm | app.platforms | app.credentials | app.render

Higher may import lower. `app.llm` importing `app.agent` is upward, and it is
what the provider boundary stops being a boundary.

Asserted by importing in a clean interpreter and looking at what came with it,
rather than by reading the source: an import added three modules deep is still
an import, and `sys.modules` is what actually happened.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _modules_pulled_in_by(target: str) -> set[str]:
    """Every `app.*` module that importing `target` drags in with it."""
    source = f"""
        import sys
        import {target}  # noqa: F401
        print(" ".join(sorted(m for m in sys.modules if m.startswith("app."))))
    """
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def test_the_provider_boundary_does_not_reach_up_into_the_agent():
    """`app.llm` hands out a model; it does not assemble an agent.

    The README states the contract -- "app/llm/ is the only package that may
    import a provider SDK. Everything else takes a BaseChatModel" -- and the
    inversion is what broke it: `agent_creation` lived in `app/llm/factory.py`,
    so the package whose job is knowing about providers had to import the
    tools and the prompt, one layer up, to do it.
    """
    pulled = _modules_pulled_in_by("app.llm.factory")

    reaching_up = {m for m in pulled if m.startswith("app.agent")}
    assert not reaching_up, (
        "app.llm must not import app.agent (layers contract); found: "
        + ", ".join(sorted(reaching_up))
    )


def test_the_agent_is_allowed_to_reach_down_to_the_model():
    """The legal direction, and the one the refactor depends on."""
    pulled = _modules_pulled_in_by("app.agent.factory")

    assert "app.llm.factory" in pulled


def test_the_package_facade_does_not_drag_the_agent_in_either():
    """`app/llm/__init__.py` re-exporting `agent_creation` was the same
    violation wearing a different hat: the provider boundary's entire public
    surface was "build me an agent"."""
    pulled = _modules_pulled_in_by("app.llm")

    assert not {m for m in pulled if m.startswith("app.agent")}
