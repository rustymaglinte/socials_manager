# Deploying to Railway

Two services from one repo and one image, plus a Postgres. They differ only in
their start command.

| Service | Start command | What it is |
|---|---|---|
| `scheduler` | `python -m app.scheduler` | Slack socket + drafts on each brand's slots |
| `publisher` | `python -m app.workers.publisher` | claims approved posts and ships them |

Both are **workers, not web services**: neither binds a port. Leave the
healthcheck path empty on both, or Railway will restart them as unhealthy —
and a restart during an approval window silently kills the run waiting in it
(see *Known limits*).

## 1. Postgres

Add a Postgres service and reference its `DATABASE_URL` from both app services.
Prefer the **internal** url (`postgres.railway.internal`) — it stays on the
private network and needs no TLS parameters. The public/proxy url works too:
`postgres://` and `?sslmode=require` are both normalised by `app/config.py`.

## 2. Environment

Copy every variable from `.env.example` into **both** services. The publisher
resolves brands and Page tokens exactly as the scheduler does, so it needs the
same list — including the Slack channel ids, which the brand loader reads on
boot.

Two that bite:

- **All three `SLACK_*_CHANNEL_ID` must be set**, including brands you are not
  drafting for. `start_listener` refuses to boot otherwise.
- **They must be distinct.** The same id in two variables silently binds two
  brands to one channel and the first match wins — a brand-isolation break that
  looks exactly like working software.

## 3. Migrations

Set a **pre-deploy command on the `publisher` service only**:

```
alembic upgrade head
```

On one service rather than both, deliberately: two containers running
`alembic upgrade head` against one database at the same moment is a race worth
not having. Nothing else runs migrations — both entry points call `sync_brands()`
on boot, which inserts into a table that has to already exist, so an unmigrated
database fails at startup rather than at first use.

## 4. Build

Both services build from the repo `Dockerfile`. It is a Dockerfile rather than a
buildpack because the post graphic is a Chromium screenshot: the browser and its
system libraries have to be in the image, and no Python buildpack puts them
there. Their absence would surface as a failed render on every post rather than
as a build error.

`.dockerignore` matters more than usual here — without it `COPY . .` drops the
developer's Windows `.venv` on top of the Linux one, producing an image whose
interpreter does not run for a reason the build log never mentions.

## Known limits

Carried over from the pre-deploy review; none is a build problem, all three will
be visible during UAT.

- **A redeploy during an approval window loses that run.** The LangGraph
  checkpointer is `InMemorySaver` and the pending-approval registry is in
  process memory, so a restart drops the in-flight run and leaves a Slack
  message whose buttons still look live. Already-approved posts are unaffected —
  they are rows, and the publisher is a separate process. Deploy outside slot
  windows (PinoySing drafts 09:00–17:00 Asia/Manila, every 2h) until the
  Postgres checkpointer replaces it.
- **The kill switches do not work as deployed.** `PAUSE_PUBLISHING` and
  `PAUSE_DRAFTING` are files, and the filesystem is ephemeral. Point
  `PUBLISHING_KILL_SWITCH` / `DRAFTING_KILL_SWITCH` at a path on a mounted
  volume if you want FR-15's lever to survive a redeploy.
- **Anyone in a brand's Slack channel can approve a post.** The approver's id is
  recorded but never checked. Acceptable in a private UAT channel; close it
  before the live Page id goes back into `brands/pinoysing/brand.yaml`.

## Before going live

`brands/pinoysing/brand.yaml` currently points at the **test** Page
(`2222222222222222`), with the live id commented out beside it. That is correct
for UAT. Swapping it back is a deliberate, separate change.
