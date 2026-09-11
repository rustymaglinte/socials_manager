"""The agent: its tools, its prompt, and the composition that joins them to a model.

`agent_creation` is the entry point a composition root wants; it takes the brand
this run is bound to (SPECS D3) and returns a compiled graph.
"""

from app.agent.factory import agent_creation

__all__ = ["agent_creation"]
