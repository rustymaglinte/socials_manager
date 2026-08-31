"""The provider boundary — the only package that may import a provider SDK.

Exports only what exists. The D11 surface (`Route`, `parse_route`,
`capabilities_for`, `Capabilities`) belongs here too, but `capabilities.py`,
`roles.py`, and `middleware.py` are still empty; re-export them as they land.
Naming them early made every `app.llm.*` import fail at package init.
"""

from app.llm.factory import agent_creation

__all__ = ["agent_creation"]
