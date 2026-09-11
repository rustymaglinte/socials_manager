"""The provider boundary — the only package that may import a provider SDK.

Exports a model, not an agent. `agent_creation` used to live here, which put
`app.agent`'s tools and prompts on this package's import list and broke the
layers contract; it is `app.agent.factory`'s now. What belongs here is anything
that has to name a provider, and nothing that merely uses one.

Still to land: the D11 surface (`Route`, `parse_route`, `capabilities_for`,
`Capabilities`). `capabilities.py`, `roles.py` and `middleware.py` are empty
files -- re-export them as they arrive. Naming them early made every
`app.llm.*` import fail at package init.
"""

from app.llm.factory import chat_model

__all__ = ["chat_model"]
