"""Assemble the agent for one brand: a model, its tools, its prompt, its gate.

Here rather than in `app.llm`, which is where it used to live. The tools and the
system prompt are both `app.agent`'s own, so building the agent from `app.llm`
meant importing upward through the layer stack -- the one contract in
pyproject.toml that had never passed. Moving the composition to the layer that
owns two of its three ingredients makes every import here point downward:
`app.llm` for the model, `app.domain` for the brand.

What `app.llm` hands over is a `BaseChatModel`, and that is the whole of the
provider boundary from this side. Nothing in this module knows or can ask which
provider answered.
"""

import logging

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.prompts.assembly import render
from app.agent.tools.submit_for_approval import submit_for_approval
from app.agent.tools.web_search import web_search
from app.domain.brand import BrandContext
from app.llm.factory import chat_model

logger = logging.getLogger(__name__)


def agent_creation(brand: BrandContext) -> CompiledStateGraph:
    """Create the agent for one brand, with its tools and middleware.

    `brand` is resolved before this is called (SPECS D3) and is the only thing
    that varies per run: it supplies the brand block appended to the shared
    prefix -- voice, the accounts that may be targeted, hashtags, banned terms.
    Without it the model has the rules but not the brand they apply to.
    """
    return create_agent(
        model=chat_model(),
        tools=[web_search, submit_for_approval],
        system_prompt=render(brand),
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "submit_for_approval": {
                        "allowed_decisions": ["approve", "reject", "edit"]
                    },
                },
                description_prefix="Post ready to review",
            )
        ],
        # In memory, so an interrupt survives only as long as this process. A
        # redeploy mid-approval therefore loses the run -- see DEPLOY.md, Known
        # limits. `langgraph-checkpoint-postgres` is already a dependency for
        # when that stops being acceptable.
        checkpointer=InMemorySaver(),
    )
