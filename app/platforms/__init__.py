"""Platform adapters, and the inventory of which ones exist.

`PUBLISHABLE_PLATFORMS` is not a list of platforms the app knows how to *write*
for -- the system prompt carries drafting guidance for four -- but of the ones a
publisher adapter exists for. That is the difference between a post that goes
out and one that dead-letters at `app/workers/publisher.py` with "No publisher
for platform 'x'", which is why a run's targets are filtered through it before
the model is ever briefed.

Deliberately a bare frozenset rather than a scan of this package: the adapter
functions live in `app.workers.publisher` (they need brand loading and
credentials, which an adapter must not know about), so the registry there is the
thing this has to agree with. A test asserts they do.

No adapter is imported here. Reading this constant must not drag httpx and a
Graph client into a process that only wanted to know what it could target.
"""

PUBLISHABLE_PLATFORMS: frozenset[str] = frozenset({"facebook"})

__all__ = ["PUBLISHABLE_PLATFORMS"]
