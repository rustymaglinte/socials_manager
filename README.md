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

| Brand | Platforms declared | Tier | Publishing today |
|---|---|---|---|
| personal | LinkedIn | personal | nothing — handle is `TODO`, no LinkedIn adapter |
| pinoysing | FB Page, X, YouTube | brand | Facebook Page (live) |
| derekt | FB Page, X, YouTube | brand (strict policy) | nothing — every id is `TODO` |

Facebook is the only adapter (`PUBLISHABLE_PLATFORMS`), so X and YouTube accounts
are declared but never briefed.

Brand is **runtime context, not a tool argument** — it resolves from the Slack
channel before the model is invoked. A thread in `#social-pinoysing` cannot name a
Derekt account, because those accounts were never in context.

## Boundaries

Enforced by `lint-imports` (see `pyproject.toml`), not by convention:

- `app/domain/` imports no framework. Not LangChain, not Slack, not a web framework.
- `app/platforms/` never learns what a brand is. An `if brand.slug == ...`
  inside an adapter is a bug; per-brand rules belong in a policy profile.
- `app/render/` is brand-blind for the same reason: it takes colours and
  strings, and `app/graphics/` decides whose they are.
- `app/workers/publisher.py` never talks to the model.
- `app/llm/` is the only package that may import a provider SDK. Everything
  else takes a `BaseChatModel` and asks `capabilities_for()` if it must branch.

## Layout

`(planned)` marks a package that exists as a directory but not yet as code.

```
brands/          config data, no Python — voice, cadence, policy, accounts
app/
  transports/    Slack (Socket Mode). Swappable — chat is transport, not system.
  api/           OAuth redirect receiver — stdlib, on demand      (planned)
  agent/         create_agent + HITL middleware, brief, tools (web_search,
                 submit_for_approval); in-memory checkpointer
  llm/           provider boundary — the ONLY place that names a provider
                 (factory.py only; capabilities/roles/middleware  (planned))
  domain/        brand loading, state machine  <- framework-free
                 (policy engine                                   (planned))
  graphics/      brand + angle -> post card (the brand-aware half)
  render/        HTML/CSS template -> PNG via Chromium (brand-blind)
  platforms/     facebook; x, linkedin, youtube                   (planned)
  credentials/   env-var lookup today; encrypted vault            (planned)
  workers/       publisher; metrics, token_refresher, quota       (planned)
  store/         models, brand-scoped repositories, migrations
                 (Postgres checkpointer                           (planned))
  scheduler.py   Slack socket + drafting on each brand's slots
  main.py        Slack socket alone, or one run for one brand
scripts/         fb_publish.py — post/list/show against a Page, no agent
tests/
```

`app/platforms/PUBLISHABLE_PLATFORMS` is the honest inventory: a brand may
declare an X account, but a run is only ever briefed for platforms that have a
publisher adapter behind them.

## LLM routing

Routed through **OpenRouter**. One model, built in
[app/llm/factory.py](app/llm/factory.py) from three variables:

```
OPENROUTER_MODEL       # the id, from https://openrouter.ai/models
OPENROUTER_PROVIDER    # what init_chat_model is told to construct
OPENROUTER_API_KEY
```

**Planned, not built.** The design below is the intended shape; the modules it
names are empty files today, and this section is written in the future tense on
purpose — an earlier version of it described them as though they existed, which
is how `TAVILY_API_KEY` came to be undocumented while eight variables nothing
reads were listed as required.

- Three roles (`LLM_CHAT_MODEL`, `LLM_VARIANT_MODEL`, `LLM_CLASSIFY_MODEL`),
  each swappable independently. Today all three would be one model.
- `llm/capabilities.py` keying on the **gateway × family** pair, because
  `openrouter:anthropic/...` and `anthropic:...` run the same model with
  different caching mechanisms.
- `llm/middleware.py` translating `Segment.cache_after` into whatever the route
  wants. [app/agent/prompts/assembly.py](app/agent/prompts/assembly.py) already
  computes those cache breakpoints; `render()` then joins the text and drops
  them, because there is nothing yet to hand them to. **Prompt caching is not
  currently in effect**, whatever the segment boundaries imply.

Caching is the one thing that does not port, and the design leans on it hard.
**Verify it empirically** when it lands — see [app/llm/README.md](app/llm/README.md).

## Post lifecycle

```
DRAFT -> PENDING_APPROVAL -> APPROVED -> SCHEDULED -> PUBLISHING -> PUBLISHED
              |                  |                         |
           REJECTED          CANCELLED              FAILED -> retry -> dead_letter
```

## Processes

Long-running — two, and they are the whole of it:

```
uv run python -m app.scheduler          # Socket Mode bot + draft on each brand's slots
uv run python -m app.workers.publisher  # claim + publish on schedule
```

(`app.workers.token_refresher` is in the build order below, not in the tree.
LinkedIn's 60-day expiry will force it; nothing publishes to LinkedIn yet.)

`uv run` rather than bare `python` because `[tool.uv] package = false` — the
project is never installed, `app` is imported from the working directory, so
these must be run from the repo root either way. Bare `python -m ...` is
equivalent once `.venv` is activated; `uv run` works without activating and
re-syncs if a dependency drifted.

Two processes is the whole of it: `app.scheduler` holds the Slack socket and
`app.workers.publisher` ships what you approve. `uv run python -m app.main` (no
argument) is the same bot without the timer, for running mentions alone;
`uv run python -m app.main <brand>` briefs that brand once and exits. Both
long-running processes also take `--once` for an external timer.

`app.scheduler` is the cron half: it briefs a brand at each slot its `brand.yaml`
declares, so every post is researched at the moment it is wanted rather than
batched in advance. Slots come from `cadence` — `every_hours` and `first_slot`,
bounded by `max_per_day` — on the brand's own clock:

```yaml
cadence:
  max_per_day: 5      # -> 09:00, 11:00, 13:00, 15:00, 17:00 Asia/Manila
  every_hours: 2
  first_slot: "09:00"
```

Mentioning the bot still works at any hour — the slot schedule gates only what
the timer starts, never what you ask for. A mention inside a slot's window does
consume that slot, since "has this slot been drafted" is a row count rather than
a flag, which is what keeps `max_per_day` honest.

A brand without those two keys is driven by hand, so this is opt-in per brand.
The reviewer gets most of the gap to the next slot to decide (90 minutes at a 2h
cadence) rather than the 10 minutes a mention-driven run allows — and a slot
missed by more than 45 minutes is skipped rather than caught up, because a
stale post is worse than no post. `New-Item PAUSE_DRAFTING` halts drafting
without stopping the publisher; approved posts keep going out.

It lives beside `app.main` rather than under `app/workers/` because the layers
contract makes `app.agent` and `app.workers` independent siblings — nothing in
`workers/` may invoke the model.

Planned, on demand — binds a port only for the length of one authorize dance,
then exits. Refresh is server-to-server and needs no callback, so this would run
about once per account per year. `app/api/` is an empty package today; Facebook
Page tokens are set directly in the environment, so nothing needs it yet:

```
uv run python -m app.api.connect <brand> <platform>   # OAuth redirect receiver
```

## Environment

**[`.env.example`](.env.example) is the list**, not this section. Copy it to
`.env` (gitignored; never commit) and fill it in.

It is checked rather than maintained by hand:
[tests/test_env_example.py](tests/test_env_example.py) scans the source for
every variable the code reads with no fallback and fails if one is undocumented,
or if a real-looking value ever lands in the committed template. This paragraph
used to be a hand-written list instead, and it had drifted badly — it required
eight variables nothing reads, and omitted `TAVILY_API_KEY`, without which the
agent cannot start.

Two that are easy to get wrong, and neither fails in an obvious way:

- **Every brand directory needs a `SLACK_<SLUG>_CHANNEL_ID`**, including brands
  you are not drafting for. `start_listener` refuses to boot otherwise.
- **They must be distinct.** The channel decides the brand (SPECS D3), so two
  brands sharing an id means one silently answers for the other. `start_listener`
  now refuses that too, rather than discovering it in a published post.

Facebook Page tokens are `FB_PAGE_TOKEN_<SLUG>`, one per brand, resolved by
[app/credentials/tokens.py](app/credentials/tokens.py). Nothing reads a YouTube,
X or LinkedIn credential yet; when the YouTube adapter lands, keep Google's
downloaded OAuth client JSON in `secrets/` (gitignored) or outside the repo,
never in the project root.

## Platform constraints worth remembering

- **Facebook personal profiles cannot be posted to via API** (`publish_actions`
  removed 2018). Pages only.
- **The Meta app must be in Live mode.** A Development-mode app can post to a
  Page its developer administers, but those posts are visible only to people
  with a role on the app — the Page owner sees them, followers do not. Live mode
  needs a privacy policy URL ([PRIVACY_POLICY.md](PRIVACY_POLICY.md)), not App
  Review: Standard Access to `pages_manage_posts` covers Pages you administer.
- **LinkedIn** personal posting is self-serve (`w_member_social`, no partner
  review), but tokens expire in **60 days** — the refresher is not optional.
- **YouTube** quota is 10,000 units/day per *project*, shared across both
  channels; `videos.insert` costs ~1,600 (~6 uploads/day).
- **Meta App Review** is only needed to post to Pages other people own, which
  this product never does.

## Build order

1. `domain/` — models, state machine, migrations — **done**
2. One adapter end-to-end, published from a script — no agent — **done**
   (Facebook, via `scripts/fb_publish.py`, rather than the X planned)
3. `workers/publisher.py` + scheduling — **done**
4. Agent with read-only tools — **done** (`web_search`)
5. Draft + approval tools, HITL interrupt — **done** (`submit_for_approval`)
6. Remaining adapters — X, LinkedIn, YouTube not started
7. Approval UI — Slack Block Kit (approve / edit / reject) is what exists

Steps 1-3 are the product. If the agent layer turned out to be a bad idea,
you'd still have a working scheduler.
