# app/llm — the provider boundary

The only package allowed to import a provider or gateway SDK. Enforced by the
"Provider SDKs are confined to app.llm" contract in `pyproject.toml`.

## What exists today

One function: `chat_model()` in [factory.py](factory.py), which builds a single
model through OpenRouter from three variables and caches it:

```
OPENROUTER_MODEL=anthropic/claude-...   # id from https://openrouter.ai/models
OPENROUTER_PROVIDER=...                 # what init_chat_model constructs
OPENROUTER_API_KEY=...
```

`capabilities.py`, `roles.py` and `middleware.py` are empty files. Everything
below — routes, roles, per-route caching — is the **planned** design, and none
of the `LLM_*` variables it names is read by anything yet. Prompt caching is not
in effect.

## Two axes, not one (planned)

A gateway like OpenRouter separates *who we send HTTP to* from *whose model
runs*. Capability differs along both, so `Route` carries both:

| Spec | gateway | family | caching |
|---|---|---|---|
| `anthropic:claude-opus-5` | direct | anthropic | `anthropic_middleware` |
| `openrouter:anthropic/claude-...` | openrouter | anthropic | `cache_control_blocks` |
| `openrouter:openai/gpt-...` | openrouter | openai | `automatic` |
| `openai:gpt-...` | direct | openai | `automatic` |

The first two rows run the *same model* with *different* caching mechanisms.
That is the whole reason this module exists.

## Configuration (planned)

Replaces the three `OPENROUTER_*` variables once roles land:

```
LLM_CHAT_MODEL=openrouter:anthropic/claude-...
LLM_VARIANT_MODEL=openrouter:anthropic/claude-...
LLM_CLASSIFY_MODEL=openrouter:openai/gpt-...-mini
OPENROUTER_API_KEY=...
```

Exact OpenRouter model IDs change; get them from <https://openrouter.ai/models>
rather than guessing. The prefix before `/` must be the real family name — it is
what selects the capability row.

## ⚠ Verify caching empirically before trusting it

This app resends a frozen tool list, a shared system prefix, and a brand block on
**every turn**. Caching is a direct multiplier on the bill, not a rounding error.

OpenRouter forwards `cache_control` and uses sticky routing to keep hits warm.
But multiple projects have reported cache markers being dropped on the
OpenAI-compat wire path when routing to Anthropic — the markers never reach the
upstream provider, and nothing errors. You just quietly pay full price.

So: **check `response.usage` for cached-token counts on a repeated request.**
If it is zero across repeats, the markers are not landing. Options in order:

1. Confirm breakpoints are actually being written by `agent/prompts/assembly.py`
   (today it computes them and `render()` drops them — nothing consumes them yet)
2. Check whether the route supports the native wire format
3. Fall back to `LLM_CHAT_MODEL=anthropic:...` direct for the chat role only —
   the role split exists precisely so this is a one-variable change

Keep one direct-provider extra installed so that fallback is always available,
and so you can A/B the gateway against it.

## Adding a gateway or family

Two edits, both in this package:

1. A row in `capabilities.py` (`_DIRECT` or `_OPENROUTER`) — once it exists
2. A branch in `factory.chat_model` if it needs a different client class
   (today it always passes OpenRouter's `base_url`)

Nothing outside `app/llm/` changes. If you find yourself editing `agent/` to add
a provider, the boundary has leaked.
