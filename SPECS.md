# socials_manager — Specification

**Status:** draft v0.1 · **Date:** 2026-08-27 · **Owner:** Rusty Maglinte

An approval-gated social media agent managing three identities across four
platforms, driven from Slack, built on LangChain/LangGraph in Python.

---

## 1. Purpose

Reduce the manual effort of running three separate social presences without
surrendering editorial control. The agent drafts, adapts per platform,
validates, and schedules. **A human approves every outbound post.**

Explicitly *not* an autonomous posting bot. The approval gate is the product,
not a safety feature bolted on afterwards.

---

## 2. Scope

### 2.1 Brands and accounts

| Brand | Tier | LinkedIn | Facebook | X | YouTube |
|---|---|---|---|---|---|
| `personal` | personal | ✅ profile | ❌ *not possible* | — | — |
| `pinoysing` | brand | — | ✅ Page | ✅ | ✅ channel |
| `derekt` | brand | — | ✅ Page | ✅ | ✅ channel |

Seven workable accounts, four platform adapters. Facebook personal-profile
posting is impossible via API (see §7.1) — resolution pending (§10, Q1).

### 2.2 Users

Single operator (the owner). Multi-user is not a v1 requirement, but brand
scoping is implemented as tenancy so it does not need retrofitting later.

---

## 3. Functional requirements

### Drafting

- **FR-1** Operator describes a concept in a Slack channel; the agent produces a
  draft with one variant per targeted platform.
- **FR-2** Variants are adapted per platform (length, tone, hashtags, media
  shape) from that platform's declared capabilities — not hand-written rules.
- **FR-3** Operator revises conversationally ("tighten the second line"); the
  agent regenerates only the affected variant.
- **FR-4** The agent may consult prior posts and their performance when drafting.

### Validation

- **FR-5** Every variant is validated by deterministic code before it can be
  submitted: character limits, media count, aspect ratio, file size.
- **FR-6** Every variant is checked against its brand's policy profile: banned
  terms, required disclaimers, duplicate detection against recent posts.
- **FR-7** Violations return to the agent as structured tool results so it can
  self-correct without operator intervention.

### Approval

- **FR-8** No post reaches a platform without an `Approval` row recording
  approver identity and timestamp.
- **FR-9** *In-conversation approval* — the agent interrupts before submitting;
  the operator accepts, edits, or rejects inline.
- **FR-10** *Out-of-band approval* — pending drafts are reviewable later, showing
  the exact final text per platform with per-platform validation status.
- **FR-11** Rejection with feedback returns the draft to the agent for revision.

### Scheduling and publishing

- **FR-12** Approved posts are scheduled; a worker publishes them independently
  of any agent or chat session.
- **FR-13** Publishing is idempotent — no double-post under retry or restart.
- **FR-14** Failed publishes retry with backoff and dead-letter after N attempts,
  notifying the operator.
- **FR-15** A global kill switch halts all publishing without a deploy.

### Observability

- **FR-16** Post performance is polled and attributed back to the originating draft.
- **FR-17** A cross-brand calendar view answers "am I overposting this week?"

---

## 4. Constraints

- **C-1** Every outbound post is human-approved. No exceptions, no auto-publish tier.
- **C-2** Brand isolation is structural: a session bound to one brand cannot
  reference another brand's accounts.
- **C-3** LLM-provider agnostic. No provider or gateway SDK outside `app/llm/`.
- **C-4** The publishing path must not depend on the LLM being available.
- **C-5** Secrets never live in the repo. Tokens are encrypted at rest.
- **C-6** Python ≥3.11, managed with `uv`. Windows-first development.

---

## 5. Architecture decisions

### D1 — One application, not one repo per brand

Three brands share four adapters, one Meta app review, one credential refresher.
Splitting would triple maintenance for roughly 5% divergence, and would force
either three Meta app reviews or an awkwardly shared app across repos. What
actually differs between brands is voice, cadence, and policy — data, not code.

*Consequence:* `Brand` is a first-class entity; everything is brand-scoped.

### D2 — The agent does not own the publishing state machine

Drafts, approvals, and schedules are database rows behind a state machine. The
agent writes rows; a separate worker publishes them.

*Consequence:* an agent crash loses a conversation, never a scheduled post.
Approval is enforced by a foreign key rather than by prompt compliance.

### D3 — Brand is runtime context, not a tool argument

`BrandContext` resolves from the Slack channel *before* the model is invoked.
Tools never take a `brand_id` parameter, because a parameter is something the
model can get wrong.

*Consequence:* cross-brand posting is impossible, not merely discouraged.

### D4 — Flat package with enforced boundaries, not a monorepo

One `pyproject.toml`. Dependency direction is enforced by `import-linter`
contracts, which buy the one real benefit of a monorepo without the packaging
overhead — for a single deployable, that overhead is paid daily against a
benefit collected never.

*Consequence:* boundary violations fail CI. Splitting later stays cheap because
module boundaries already sit where package boundaries would go.

### D5 — `create_agent` on LangGraph; not deepagents, not raw `StateGraph`

The chat loop is three to six tool calls, then a human decides. deepagents
targets long-horizon autonomy (>10 steps, planning, virtual filesystem,
subagents); its virtual filesystem would compete with Postgres as a second
source of draft truth, and its core value is precisely the autonomy this system
deliberately removes.

*Consequence:* LangGraph is used directly for the Postgres checkpointer and
`interrupt()` / `Command(resume=...)`. Revisit deepagents only for a separate
campaign-planner entrypoint (§9).

### D6 — Slack, not Telegram

Channels map 1:1 onto brands, which gives D3 its enforcement for free. Block Kit
renders per-platform validation and the exact final text for out-of-band
approval. Threads map cleanly to LangGraph `thread_id`.

*Consequence:* more setup than Telegram and worse mobile UX. Mitigated by a
`ChatTransport` protocol keeping the swap to roughly a day.

### D7 — Provider-agnostic by capability detection, not lowest common denominator

`app/llm/` is the sole provider boundary. `capabilities_for(route)` reports what
is available; code uses a route's strengths and degrades cleanly when they are
absent.

*Consequence:* the caching *mechanism* branches by route; the prompt *structure*
that enables caching is shared.

### D11 — OpenRouter as the default gateway; capability keyed on (gateway, family)

One key, one bill, one string to change per model — the natural fit for C-3, and
it removes the need for three provider accounts.

A gateway splits "provider" into two independent axes: **gateway** (who receives
the HTTP request) and **family** (whose model actually runs).
`openrouter:anthropic/claude-...` and `anthropic:claude-...` run the same model
but do not share a caching mechanism, so a single-axis capability table was
wrong. `Route(gateway, family, model)` replaces it.

*Consequence:* no structural change beyond `app/llm/` — `agent/`, `domain/`,
`platforms/`, and `workers/` are untouched, which is the boundary earning its keep.

*Risk:* caching is the exposure. Cache markers have been reported dropped on the
OpenAI-compat wire path when routing to Anthropic — silently, with no error. This
app resends a frozen tool list, a shared system prefix, and a brand block every
turn, so a silent caching failure is a direct cost multiplier. Mitigation: verify
`response.usage` cached-token counts on repeated requests; keep one direct
provider installed so the chat role can fall back with a one-variable change.
See §10 Q7.

### D8 — Platform is a tool argument, not a tool

`post_to_x` / `post_to_linkedin` / … would mean N×M tool explosion, cache churn,
and mis-selection. Platform capabilities go into context instead.

*Consequence:* about ten tools total, frozen for prompt-cache stability.

### D9 — Validation is deterministic code, never the model

Character counts and aspect ratios are computed, not reasoned about.

*Consequence:* the model handles voice and judgment only. Cheaper and reliable.

### D10 — Variant generation is structured-output fan-out, not an agent loop

Adapting one approved concept to four platforms is four independent parallel
structured calls. The next step is already known, so no agent is needed.

---

## 6. Domain model

```
Brand ──< Account ──< ScheduledPost
  │                        │
  └──< Draft ──< PostVariant
         │
         └──< Approval
```

| Entity | Key fields |
|---|---|
| `Brand` | `slug`, `tier`, `policy_profile` |
| `Account` | `brand_id`, `platform`, `external_id`, `enabled` |
| `Draft` | `brand_id`, `concept`, `state` |
| `PostVariant` | `draft_id`, `platform`, `body`, `media[]` |
| `Approval` | `draft_id`, `approver`, `decision`, `at` |
| `ScheduledPost` | `variant_id`, `account_id`, `scheduled_for`, `state`, `external_id` |

Every table carries `brand_id NOT NULL`. Repositories take `brand_id` in every
query signature, with optional Postgres row-level security as a second layer.

### 6.1 Lifecycle

```
DRAFT → PENDING_APPROVAL → APPROVED → SCHEDULED → PUBLISHING → PUBLISHED
             │                 │                       │
          REJECTED         CANCELLED           FAILED → retry → DEAD_LETTER
```

Transitions are enforced in `app/domain/states.py`, never in tools.

---

## 7. Platform constraints

### 7.1 Facebook

Personal-profile posting was removed with `publish_actions` in Graph API v3.0
(2018) and has not returned. **Pages only.** One Meta app covers both brand
Pages; `pages_manage_posts` review is the longest lead time in the project and
should be started before adapter code is written.

### 7.2 LinkedIn

Personal posting is self-serve via `w_member_social` plus the "Share on
LinkedIn" product — no partner review required. Access tokens expire in **60
days** (refresh tokens 365), which makes the token refresher a v1 requirement
rather than a later nicety. Roughly 100 calls/day per member.

### 7.3 YouTube

Quota is 10,000 units/day **per project**, shared across both channels.
`videos.insert` costs about 1,600 units, giving roughly six uploads/day total.
An increase is requestable.

### 7.4 Slack

`conversations.history` and `conversations.replies` are limited to one request
per minute for non-Marketplace apps as of 2026-03-03. Internal custom apps are
excluded, and an event-driven bot does not poll history. **Do not architect
around reading back channel history** — the transcript lives in our checkpointer.

---

## 8. Tool surface

Frozen at about ten tools for prompt-cache stability.

| Tool | Gated | Notes |
|---|---|---|
| `list_accounts` | no | this brand's accounts only |
| `get_calendar` | no | |
| `get_post_performance` | no | |
| `search_past_posts` | no | |
| `get_brand_guidelines` | no | |
| `create_draft` | no | a draft is inert |
| `revise_variant` | no | |
| `submit_for_approval` | **yes** | the interrupt point |
| `reschedule` | **yes** | |
| `cancel_scheduled` | **yes** | |

Gating the smallest possible set keeps the chat usable; a draft that cannot
reach a platform does not need a gate.

---

## 9. Out of scope for v1

- Instagram and TikTok adapters
- Autonomous or unattended posting of any kind
- Multi-user accounts, roles, delegated approval
- Comment and DM handling, social listening
- Paid promotion and ad management
- Media generation (images, video, thumbnails)
- Long-horizon campaign planning — candidate for a separate deepagents entrypoint

---

## 10. Open questions

- **Q1** Personal Facebook: create a Page, or accept that it stays manual? Blocks
  the `personal` brand's account list.
- **Q2** Final brand names and handles — `brands/*/brand.yaml` are all `TODO`.
- **Q3** Voice profiles for all three brands are unwritten (`brands/*/voice.md`).
- **Q4** Where does the out-of-band approval UI live — Slack Block Kit modals
  only, or a minimal web view? Slack-only is assumed until proven insufficient.
- **Q5** Media pipeline: where do images and video live before upload? Local
  `media/` is assumed for v1; object storage if it outgrows that.
- **Q6** Derekt compliance rules need review by someone qualified. Derekt is a
  trading SaaS (software sold to traders), not a broker or advisory — but
  marketing claims about outcomes are still exposed. The
  `strict` policy profile is a placeholder, not legal advice.
- **Q7** Does prompt caching actually land through OpenRouter to Anthropic? Must
  be measured (`response.usage`, repeated request) before the cost model in D11
  can be trusted. Blocks nothing, but a silent failure is expensive.

---

## 11. Build order

1. `domain/` — models, state machine, migrations
2. One adapter (X) end-to-end, published from a script — no agent
3. `workers/publisher.py` — scheduling, claiming, idempotency
4. Agent with read-only tools
5. Draft and approval tools, HITL interrupt
6. Remaining adapters (LinkedIn → Facebook → YouTube)
7. Out-of-band approval UI

Steps 1–3 are the product. If the agent layer proved to be a bad idea, a working
scheduler would remain.
