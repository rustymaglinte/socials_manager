import logging
import os
from typing import Any, Dict

from dotenv import load_dotenv
from langchain.tools import tool
from tavily import AsyncTavilyClient

load_dotenv()  # Load environment variables from .env file

logger = logging.getLogger(__name__)

# Async client: a search is the one genuinely slow thing the agent does on its
# own, and blocking the loop for it would also stall the Slack listener sharing it.
tavily_client = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


@tool
async def web_search(query: str) -> Dict[str, Any]:
    """
    Use this tool to perform a web search and retrieve relevant information.
    """
    logger.info("Performing web search for query: %s", query)
    return await tavily_client.search(query=query, limit=3, search_depth="advanced")
