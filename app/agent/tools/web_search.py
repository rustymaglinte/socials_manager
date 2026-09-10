"""The one tool that reaches the open internet.

The key is read when a search happens, not when this module is imported, and
that is the whole of what is interesting here. Built at import, a missing
TAVILY_API_KEY raised out of the import statement -- which took down
`app.llm.factory`, `app.main`, and everything reaching them, before any of the
machinery that could report it usefully had been constructed. What an operator
saw was a traceback through an import chain; what they needed was a sentence
naming the variable they had missed.

Deferred, the same mistake is a run that fails with that sentence, in the
channel the brief came from, leaving every other module importable.
"""

import logging
import os
from functools import lru_cache
from typing import Any, Dict

from dotenv import load_dotenv
from langchain.tools import tool
from tavily import AsyncTavilyClient

load_dotenv()  # Load environment variables from .env file

logger = logging.getLogger(__name__)

ENV_VAR = "TAVILY_API_KEY"


class WebSearchUnavailable(RuntimeError):
    """No search key, so this run cannot research anything.

    A RuntimeError rather than a SystemExit: the run is over, but the process
    is not -- the Slack listener stays up and the publisher keeps shipping what
    was already approved. Names the variable, because this is a configuration
    mistake an operator makes once per deployment and the useful answer is
    always "open that and set this".
    """


@lru_cache(maxsize=1)
def _client_for(api_key: str) -> AsyncTavilyClient:
    """One client per key. Cached on the key rather than outright, so a rotated
    variable is picked up without a restart -- the same trade
    `app.credentials.tokens.credentials_for` makes."""
    return AsyncTavilyClient(api_key=api_key)


def client() -> AsyncTavilyClient:
    """The search client, or say which variable is missing.

    An agent searches several times in a run, so the client is reused: a fresh
    one per search would mean a fresh connection pool per search.
    """
    api_key = (os.getenv(ENV_VAR) or "").strip()
    if not api_key:
        raise WebSearchUnavailable(
            f"Set {ENV_VAR} in .env (or in the deployment's environment) -- "
            f"this run needed to search the web and could not. Nothing was "
            f"drafted; anything already approved is unaffected."
        )
    return _client_for(api_key)


@tool
async def web_search(query: str) -> Dict[str, Any]:
    """
    Use this tool to perform a web search and retrieve relevant information.
    """
    logger.info("Performing web search for query: %s", query)
    return await client().search(query=query, limit=3, search_depth="advanced")
