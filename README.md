# socials_manager

Approval-gated social media agent. One app, three brands, four platforms.

## Shape

Three planes, deliberately separated:

| Plane | Owns | Lifetime |
|---|---|---|
| Conversation | Slack transport, LangGraph agent, checkpointed threads | per-thread |
| Content | Draft -> Approval -> Schedule -> Publish | durable, outlives sessions |
| Platform | Adapters, credentials, capabilities, quota | long-lived |

**The agent does not own the publishing state machine.** It writes rows; a
worker publishes them. If the agent process dies, approved posts still ship.

A post becomes publishable only when an `Approval` row exists with a real
approver identity. The agent cannot bypass that — it's a foreign key, not a prompt.

## Brands

| Brand | Platforms | Tier |
|---|---|---|
| personal | LinkedIn | personal |
| pinoysing | FB Page, X, YouTube | brand |
| derekt | FB Page, X, YouTube | brand (strict policy) |

Brand is **runtime context, not a tool argument** — it resolves from the Slack
channel before the model is invoked. A thread in `#social-pinoysing` cannot name a
Derekt account, because those accounts were never in context.

## Boundaries

Enforced by `lint-imports` (see `pyproject.toml`), not by convention:

- `app/domain/` imports no framework. Not LangChain, not Slack, not a web framework.
- `app/platforms/` never learns what a brand is. An `if brand.slug == ...`
  inside an adapter is a bug; per-brand rules belong in a policy profile.
- `app/workers/publisher.py` never talks to the model.
- `app/llm/` is the only package that may import a provider SDK. Everything
  else takes a `BaseChatModel` and asks `capabilities_for()` if it must branch.

## Layout

```
brands/          config data, no Python — voice, cadence, policy, accounts
app/
  transports/    Slack (Socket Mode). Swappable — chat is transport, not system.
  api/           OAuth redirect receiver — stdlib, on demand, not a service
  agent/         create_agent + middleware stack
  llm/           provider boundary — the ONLY place that names a provider
  domain/        models, state machine, policy engine  <- framework-free
  platforms/     x, linkedin, facebook, youtube        <- brand-blind
  credentials/   encrypted token vault + refresh schedules
  workers/       publisher, metrics, token_refresher, quota
  store/         brand-scoped repositories, checkpointer, migrations
tests/
```

## LLM routing

Provider-agnostic by construction, not by avoiding features. Routed through
**OpenRouter** by default. Three roles, each swappable with one env var:

```
LLM_CHAT_MODEL=openrouter:anthropic/claude-...      # agent loop
LLM_VARIANT_MODEL=openrouter:anthropic/claude-...   # per-platform copy
LLM_CLASSIFY_MODEL=openrouter:openai/gpt-...-mini   # tagging, metric summaries
```

`uv sync --extra openrouter`, plus one direct provider extra to fall back to and
A/B against. Model IDs from <https://openrouter.ai/models>.

A gateway splits "provider" into two axes — **gateway** (who we send HTTP to) and
**family** (whose model runs) — and capability differs along both.
`openrouter:anthropic/...` and `anthropic:...` run the same model with different
caching mechanisms. `llm/capabilities.py` keys on the pair.

Caching is the one thing that does not port, and this app leans on it hard.
**Verify it empirically** — see [app/llm/README.md](app/llm/README.md).

## Post lifecycle

```
DRAFT -> PENDING_APPROVAL -> APPROVED -> SCHEDULED -> PUBLISHING -> PUBLISHED
              |                  |                         |
           REJECTED          CANCELLED              FAILED -> retry -> dead_letter
```

## Processes

Long-running:

```
python -m app.transports.slack           # Socket Mode bot
python -m app.workers.publisher          # claim + publish on schedule
python -m app.workers.token_refresher    # LinkedIn tokens expire in 60d
```

On demand — binds a port only for the length of one authorize dance, then exits.
Refresh is server-to-server and needs no callback, so this runs about once per
account per year:

```
python -m app.api.connect <brand> <platform>   # OAuth redirect receiver
```

## Environment

Set these in your shell or a local `.env` (gitignored; never commit):

```
# Provider key — set whichever provider LLM_*_MODEL points at
ANTHROPIC_API_KEY
DATABASE_URL
CREDENTIAL_ENCRYPTION_KEY     # Fernet key for the token vault
SLACK_BOT_TOKEN               # xoxb-
SLACK_APP_TOKEN               # xapp- (Socket Mode)
SLACK_PERSONAL_CHANNEL_ID     # one C0... id per brand: SLACK_<SLUG>_CHANNEL_ID.
SLACK_DEREKT_CHANNEL_ID       # The channel decides the brand (SPECS D3), so a
SLACK_PINOYSING_CHANNEL_ID    # wrong id here is a brand-isolation break.
META_APP_ID / META_APP_SECRET
X_CLIENT_ID / X_CLIENT_SECRET
LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
```

Google hands you the YouTube OAuth client as a downloaded JSON file. Do not
leave it in the project root. Put it in `secrets/` (gitignored) or outside the
repo, and point at it by absolute path:

```
GOOGLE_CLIENT_SECRETS_FILE=C:\Users\...\secrets\client_secret.json
```

## Platform constraints worth remembering

- **Facebook personal profiles cannot be posted to via API** (`publish_actions`
  removed 2018). Pages only.
- **LinkedIn** personal posting is self-serve (`w_member_social`, no partner
  review), but tokens expire in **60 days** — the refresher is not optional.
- **YouTube** quota is 10,000 units/day per *project*, shared across both
  channels; `videos.insert` costs ~1,600 (~6 uploads/day).
- **Meta app review** for `pages_manage_posts` is the longest lead time in the
  project. Start it before writing adapter code.

## Build order

1. `domain/` — models, state machine, migrations
2. One adapter (X) end-to-end, published from a script — no agent
3. `workers/publisher.py` + scheduling
4. Agent with read-only tools
5. Draft + approval tools, HITL interrupt
6. Remaining adapters
7. Approval UI

Steps 1-3 are the product. If the agent layer turned out to be a bad idea,
you'd still have a working scheduler.
