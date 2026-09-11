"""The provider boundary: build the chat model, and nothing else.

This module used to assemble the agent as well -- tools, system prompt, HITL
middleware, checkpointer -- which meant the package whose job is knowing about
providers had to import `app.agent.tools` and `app.agent.prompts` to do it. That
is upward through the layer stack in pyproject.toml, and it is the one contract
of the six that had never passed.

The inversion is the part worth naming, because the error message only shows the
symptom. `app.llm` exists so that the rest of the app does not have to know
which provider is behind the model (README: "app/llm/ is the only package that
may import a provider SDK. Everything else takes a BaseChatModel"). A boundary
whose public surface is `agent_creation` is not that boundary -- it is a
composition root that happens to live here. So the composition moved to
`app.agent.factory`, where the tools and prompts already are, and this module
went back to answering one question: which model, from which provider.

Nothing here is read at import. The model is built on first call and cached,
the same shape as `app.config.settings` and `app.agent.tools.web_search.client`
-- so a missing OPENROUTER_* variable is a run that fails saying so, rather than
an ImportError arriving from three modules away.
"""

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

load_dotenv()

logger = logging.getLogger(__name__)

# The gateway everything is routed through. `MODEL` is the id from
# https://openrouter.ai/models; `PROVIDER` is what init_chat_model is told to
# construct. Named here so the three of them cannot disagree about spelling.
BASE_URL = "https://openrouter.ai/api/v1"
MODEL_VAR = "OPENROUTER_MODEL"
PROVIDER_VAR = "OPENROUTER_PROVIDER"
API_KEY_VAR = "OPENROUTER_API_KEY"


@lru_cache(maxsize=1)
def chat_model() -> BaseChatModel:
    """The model the agent runs on, built once.

    A `BaseChatModel` and not something of ours: that return type *is* the
    contract. A caller can invoke it, bind tools to it, and never learn which
    provider answered -- which is what lets the gateway change without anything
    above this line changing with it.
    """
    model = init_chat_model(
        model=os.getenv(MODEL_VAR),
        model_provider=os.getenv(PROVIDER_VAR),
        base_url=BASE_URL,
        api_key=os.getenv(API_KEY_VAR),
    )
    logger.info("Chat model: %s via OpenRouter", os.getenv(MODEL_VAR) or "(unset)")
    return model
