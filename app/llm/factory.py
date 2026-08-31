import logging
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from app.agent.tools.web_search import web_search
from app.agent.tools.submit_for_approval import submit_for_approval
from app.agent.prompts.assembly import render
from app.domain.brand import BrandContext

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
load_dotenv()

model = init_chat_model(
    model=os.getenv("OPENROUTER_MODEL"),
    model_provider=os.getenv("OPENROUTER_PROVIDER"),
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)


def agent_creation(brand: BrandContext) -> CompiledStateGraph:
    """Create the agent for one brand, with its tools and middleware.

    `brand` is resolved before this is called (SPECS D3) and is the only thing
    that varies per run: it supplies the brand block appended to the shared
    prefix -- voice, the accounts that may be targeted, hashtags, banned terms.
    Without it the model has the rules but not the brand they apply to.
    """

    agent = create_agent(
        model=model,
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
        checkpointer=InMemorySaver(),  # interrupts need somewhere to persist
    )

    return agent
