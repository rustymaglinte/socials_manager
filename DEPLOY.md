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
resolves brands, Page ids and Page tokens exactly as the scheduler does, so it
needs the same list — including the Slack channel ids, which the brand loader
reads on boot.

Three that bite:

- **`FB_PAGE_ID_PINOYSING` must be set on both.** Account ids are not in
  `brand.yaml`. Without it the scheduler drafts nothing for PinoySing and the
  publisher dead-letters its approved posts, each saying which variable is
  missing. It is read at boot, so changing it needs a redeploy of both.

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

Carried over from the pre-deploy review; none is a build problem.

- **A redeploy during an approval window loses that run.** The LangGraph
  checkpointer is `InMemorySaver` and the pending-approval registry is in
  process memory, so a restart drops the in-flight run and leaves a Slack
  message whose buttons still look live. Already-approved posts are unaffected —
  they are rows, and the publisher is a separate process. Deploy outside slot
  windows (PinoySing drafts at 12:00 and 19:00 Asia/Manila, each open for 3h, so
  12:00–15:00 and 19:00–22:00) until the
  Postgres checkpointer replaces it.
- **Use the variable, not the file, to halt things.** FR-15's switch has two
  spellings. The file (`PAUSE_PUBLISHING` / `PAUSE_DRAFTING`) is the local
  lever and does not work here — there is no shell to create it in and the
  filesystem is ephemeral. On Railway set the variable of the same name to `1`
  on the service you want to stop; it is read every cycle, so it takes effect
  within one poll without a restart. `PAUSE_PUBLISHING=false` deliberately does
  **not** pause, so setting it to `false` to "turn it off" does what you meant.

  The two are separate on purpose: pausing new drafts while approved posts keep
  going out is the common case.
- **Anyone in a brand's Slack channel can approve a post.** The approver's id is
  recorded but never checked. Accepted while the operator is the channel's only
  member; close it before anyone else joins a brand channel.

## Going live

Which Page PinoySing posts to is `FB_PAGE_ID_PINOYSING`: the **live** Page's id
or the test Page's. Set it per environment —
live on Railway, test locally — rather than committing a swap. Changing it on
Railway means changing it on both services and redeploying. `FB_PAGE_TOKEN_PINOYSING`
must be a token for the same Page; `python -m scripts.fb_publish check pinoysing`
compares the two.

The Meta app must also be in **Live mode**, or every post the API makes is
visible only to people with a role on the app: the Page owner sees it, followers
do not. Switching needs a privacy policy URL ([PRIVACY_POLICY.md](PRIVACY_POLICY.md),
published somewhere public), an app icon and a category, under App Settings →
Basic. No App Review — Standard Access covers Pages you administer. Check the
first post afterwards from a logged-out browser, not the owner's account.
