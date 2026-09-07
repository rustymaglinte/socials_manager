"""The tables. Rows obeying the lifecycle in `app.domain.states`.

This is the content plane (README, "Shape"): what survives when the agent
process dies. The rules live one layer down in the domain and are re-stated here
as constraints, on purpose -- a rule enforced only in Python is a rule the next
process to open this database does not know about.

Three of those re-statements are load-bearing rather than decorative:

- **Every table carries `brand_slug NOT NULL`** (SPECS 6). Tenancy is a column,
  not a join path, so a repository that forgets to scope a query is a bug you
  can grep for rather than one you have to trace.
- **`scheduled_posts` cannot exist without an approving verdict.** Not merely a
  NOT NULL `approval_id` -- the foreign key is composite, onto `(id, decision)`,
  with a CHECK pinning the decision to APPROVING_VERDICTS. That is what makes
  the README's claim ("it's a foreign key, not a prompt") literally true: a
  plain `approval_id` could point at a rejection row and satisfy the database.
- **One live schedule per variant** (FR-13), as a partial unique index. The
  adapter cannot make publishing idempotent -- Graph has no idempotency key --
  so the guarantee has to live here.

Two deviations from SPECS 6's entity list, both because `brands/<slug>/` is
already the source of truth for configuration:

- `Brand` holds a slug and nothing else. Its tier, policy profile, voice and
  cadence are in `brand.yaml`, and a second copy in Postgres is a second place
  for them to be wrong. The table earns its place purely as the foreign-key
  anchor for `brand_slug`, which a bare string column could not enforce.
- There is no `accounts` table. An account *is* `(brand, platform)`, and its
  external id already comes from `brand.yaml` -- `scripts/fb_publish.py` makes
  exactly that join today. `ScheduledPost` therefore carries `platform`, and the
  publisher resolves the page id the same way the script does. A store-side
  `Account` would be a second class of that name, loaded from somewhere else.
"""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.states import (
    APPROVING_VERDICTS,
    OCCUPYING_SCHEDULE_STATES,
    DraftState,
    ScheduleState,
    Verdict,
)

# Slugs are directory names under brands/ (`pinoysing`, `derekt`). Sized once
# here so every brand_slug column agrees; a mismatch would make the foreign keys
# fail to create on some backends rather than fail loudly here.
SLUG_LENGTH = 64
BRAND_FK = "brands.slug"

# `facebook`, `linkedin`, `x`, `youtube`. A plain string, not an enum: platforms
# arrive from brand.yaml, and adding one should be a line of config plus an
# adapter, never a migration.
PLATFORM_LENGTH = 32


class Base(DeclarativeBase):
    """Declarative base, and the metadata Alembic will autogenerate against."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    """A UUID primary key.

    UUIDs rather than serials because ids are minted by the process creating the
    row -- the agent writes a draft and its variants in one unit of work, and
    needing a round trip to learn each id would make that two.
    """
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


def _brand_slug() -> Mapped[str]:
    """The tenancy column. On every table, never nullable (SPECS 6).

    Indexed on every table because every repository query filters by it -- that
    is the whole point of the column, so the index is not speculative.
    """
    return mapped_column(
        sa.String(SLUG_LENGTH),
        # RESTRICT, not CASCADE: deleting a brand should not silently take its
        # published history with it. Retiring a brand is a decision someone
        # makes deliberately, having first dealt with the rows.
        sa.ForeignKey(BRAND_FK, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


def _state_column(enum: type[StrEnum], default: StrEnum) -> Mapped:
    """A lifecycle state column: a VARCHAR that the ORM coerces to `enum`.

    `native_enum=False` keeps this out of Postgres's own ENUM type, which cannot
    have a value removed and whose alterations lock more than a CHECK rewrite
    does. `values_callable` stores the lowercase values rather than SQLAlchemy's
    default of the member NAMES, so what sits in the column is what
    `app.domain.states` calls it.

    `create_constraint=False` because the CHECK is declared separately, by
    `_state_check` -- see there for why.
    """
    return mapped_column(
        sa.Enum(
            enum,
            native_enum=False,
            create_constraint=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=default,
    )


def _state_check(column: str, enum: type[StrEnum], name: str) -> sa.CheckConstraint:
    """The CHECK restricting a state column to the values the domain defines.

    Written out rather than left to `sa.Enum(create_constraint=True)`, which
    produces the same DDL but attaches it as a *type-bound* constraint. Alembic
    does not recognise those as part of the metadata: it sees four CHECKs in the
    database matching nothing in the models and proposes dropping them. That
    makes `alembic check` permanently red, and -- much worse -- means the next
    `--autogenerate` hands you a migration that quietly removes the validation
    on every lifecycle column.

    Declared explicitly, they compare cleanly, which also buys the thing the
    generated version could never do: adding a state to `app.domain.states` now
    shows up as real drift, so the migration has to land before a row can carry
    the new value.
    """
    values = ", ".join(f"'{member.value}'" for member in enum)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


def _created_at() -> Mapped[datetime]:
    # timezone=True throughout: this app spans a Manila audience and a UTC host
    # (see BrandContext.timezone), and a naive timestamp here would be a bug
    # that only shows up in the calendar view (FR-17).
    return mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class Brand(Base):
    """A brand's identity, and deliberately nothing else.

    Everything that makes a brand a brand -- voice, cadence, policy, accounts --
    is in `brands/<slug>/`, loaded by `app.domain.brand`. This table exists so
    `brand_slug` can be a foreign key rather than free text; without it a typo
    inserts cleanly and the row is invisible to every query that looks for it.

    Seeded from the brand directory, not hand-written.
    """

    __tablename__ = "brands"

    slug: Mapped[str] = mapped_column(sa.String(SLUG_LENGTH), primary_key=True)
    created_at: Mapped[datetime] = _created_at()

    def __repr__(self) -> str:
        return f"<Brand {self.slug}>"


class Draft(Base):
    """One concept, before it is adapted to any platform.

    Inert by design (SPECS 8: "a draft is inert") -- nothing here can reach a
    platform, which is why `create_draft` is an ungated tool.
    """

    __tablename__ = "drafts"
    __table_args__ = (_state_check("state", DraftState, "ck_drafts_state"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()

    # What the operator asked for, or the brief the angle produced.
    concept: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Which brief angle this came from, when it came from one. Nullable: a
    # concept typed into Slack has no angle. Kept because the angle decides the
    # graphic's template (BrandContext.theme.template_for), so the renderer
    # needs it back at publish time.
    #
    # Indexed because it is also the analytical dimension. "Which kinds of post
    # work for this brand" (FR-4) is a group-by on this column, reached from
    # engagement rows through scheduled_posts and post_variants -- so the angle
    # is what a well-performing post is eventually attributed to.
    angle: Mapped[str | None] = mapped_column(sa.String(64), index=True)

    # The LangGraph thread that produced this, as minted in app.main. The link
    # back from a durable row to the conversation that made it -- the only one
    # there is, since Slack history is explicitly not readable back (SPECS 7.4)
    # and the transcript lives in the checkpointer.
    thread_id: Mapped[str | None] = mapped_column(sa.String(128), index=True)

    state: Mapped[DraftState] = _state_column(DraftState, DraftState.DRAFT)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    variants: Mapped[list["PostVariant"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Draft {self.id} {self.brand_slug} {self.state}>"


class PostVariant(Base):
    """One platform's version of a draft.

    The unique constraint on (draft_id, platform) is the structural form of a
    rule `app.main` used to keep in a Python set: one brief produces one post
    per platform. There it was a guard the agent could be talked past across
    processes; here it is an integrity error.

    Which is why a revision is an update and not an insert -- see
    `app.store.repositories.drafts.record_variant`. FR-11 sends a rejected draft
    back for a rewrite, and the rewrite is these same words replaced, so the
    constraint bounds the run without standing in the way of the loop. What
    stops an *approved* platform being submitted twice is the constraint plus a
    query for its verdict, not the constraint alone.
    """

    __tablename__ = "post_variants"
    __table_args__ = (
        sa.UniqueConstraint("draft_id", "platform", name="uq_variant_draft_platform"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()
    draft_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False
    )

    platform: Mapped[str] = mapped_column(sa.String(PLATFORM_LENGTH), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Media descriptors, not media. The bytes live wherever SPECS Q5 settles;
    # this records what should be attached. JSONB on Postgres, plain JSON
    # elsewhere, so the models still import under a non-Postgres backend.
    media: Mapped[list[dict]] = mapped_column(
        sa.JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
    )

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    draft: Mapped["Draft"] = relationship(back_populates="variants")

    def __repr__(self) -> str:
        return f"<PostVariant {self.id} {self.platform}>"


class PostMedia(Base):
    """The rendered graphic for one variant, as the exact bytes a human approved.

    Bytes in a column, not a path on disk, and that is a deployment fact rather
    than a preference: the agent and the publisher are separate processes (D2)
    and on Railway they are separate services with ephemeral filesystems and
    volumes that cannot be mounted twice. A path would be written by one service
    and read by another that cannot see it -- and would not survive a redeploy
    even if they were the same one. Postgres is already the durable plane both
    of them share.

    Stored rather than re-rendered at publish time, for the reason
    `Approval.approved_body` is snapshotted rather than referenced: what goes out
    has to be what somebody said yes to. A post approved today and published
    tomorrow must not pick up an edit to `post.html` or to the brand's palette
    made in between. It also keeps Chromium out of the publisher's image, which
    is a few hundred megabytes the worker has no other reason to carry.

    A separate table rather than a column on `post_variants` so the blob is
    touched only by the two queries that want it: the claim query lists its
    columns explicitly, and nothing scanning variants drags a megabyte per row
    behind it.

    What the graphic *says* is not here -- the hook, the sub and the template
    live in `PostVariant.media`, which is the column that exists to record what
    should be attached. This table holds the attachment itself.
    """

    __tablename__ = "post_media"
    __table_args__ = (
        # One graphic per variant. A revision after a rejection replaces the
        # card rather than accumulating a second one beside it, exactly as the
        # revision replaces the variant's words.
        sa.UniqueConstraint("variant_id", name="uq_media_variant"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()
    variant_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("post_variants.id", ondelete="CASCADE"), nullable=False
    )

    image: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    # Whatever the renderer produced. A column rather than an assumption, so a
    # later template that emits something other than PNG does not need the
    # upload path to guess.
    content_type: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="image/png"
    )
    # Of `image`. Cheap to compute, and it is what lets a log line or a support
    # question establish that the bytes on the Page are the bytes in the review.
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    created_at: Mapped[datetime] = _created_at()

    def __repr__(self) -> str:
        return f"<PostMedia {self.variant_id} {self.content_type} {len(self.image or b'')}b>"


class Approval(Base):
    """A human's verdict on one variant. FR-8's evidence.

    Per variant, not per draft as SPECS 6 lists it: the reviewer is shown one
    platform's text at a time (`request_approval` takes brand, platform,
    content), so a draft-level row would claim an approval that was never
    actually given for three of the four platforms.

    Records rejections and timeouts too -- "nobody looked" is a fact worth
    keeping. Which is exactly why `scheduled_posts` cannot settle for a plain
    foreign key to this table: see `_APPROVING_CHECK` below.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        # The target of the composite foreign key from scheduled_posts. Postgres
        # requires a unique constraint on the referenced columns, and (id) alone
        # will not do -- the decision has to travel with the id for the CHECK on
        # the other side to be able to test it.
        sa.UniqueConstraint("id", "decision", name="uq_approval_id_decision"),
        _state_check("decision", Verdict, "ck_approvals_decision"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()
    variant_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("post_variants.id", ondelete="CASCADE"), nullable=False
    )

    decision: Mapped[Verdict] = _state_column(Verdict, Verdict.REJECTED)

    # Who. Nullable only for `timeout`, where there is genuinely no-one -- every
    # other verdict came from a Slack user id, and FR-8 requires the identity.
    approver: Mapped[str | None] = mapped_column(sa.String(64))

    # The text as approved, snapshotted rather than referenced. An `edited`
    # verdict replaces the content, and a variant edited again afterwards must
    # not inherit this approval -- so the publisher sends what is written here,
    # never PostVariant.body.
    approved_body: Mapped[str | None] = mapped_column(sa.Text)

    # Why it was rejected, when the reviewer gave a reason. Feeds FR-11's
    # revision loop.
    note: Mapped[str | None] = mapped_column(sa.Text)

    decided_at: Mapped[datetime] = _created_at()

    def __repr__(self) -> str:
        return f"<Approval {self.id} {self.decision} by {self.approver}>"


# The approving verdicts, as a SQL literal list, generated from the domain set
# rather than typed out -- a divergence between the two would be a gate that
# looks closed in Python and is open in the database.
_APPROVING_SQL = ", ".join(f"'{verdict.value}'" for verdict in sorted(APPROVING_VERDICTS))

# Likewise: the states in which a scheduled post still holds its variant.
_OCCUPYING_SQL = ", ".join(
    f"'{state.value}'" for state in sorted(OCCUPYING_SCHEDULE_STATES)
)


class ScheduledPost(Base):
    """An approved variant, queued for a worker to publish. SPECS D2's whole point.

    The agent writes this row and stops caring. `app.workers.publisher` claims
    it, calls the adapter, and records what happened -- with no LangChain import
    anywhere in that path (C-4, and the "Publisher never talks to the model"
    contract in pyproject.toml).
    """

    __tablename__ = "scheduled_posts"
    __table_args__ = (
        # C-1 and FR-8, in SQL. The pair of columns points at one approvals row
        # AND carries its decision, and the CHECK then refuses any decision that
        # is not an approval. Two constraints because either alone has a hole:
        # the FK alone would accept a rejection, the CHECK alone would accept a
        # decision that matches no row.
        sa.ForeignKeyConstraint(
            ["approval_id", "approval_decision"],
            ["approvals.id", "approvals.decision"],
            name="fk_scheduled_post_approved",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"approval_decision IN ({_APPROVING_SQL})",
            name="ck_scheduled_post_actually_approved",
        ),
        _state_check("state", ScheduleState, "ck_scheduled_posts_state"),
        # Narrower than the one above it: this admits any verdict the domain
        # defines, and `ck_scheduled_post_actually_approved` then cuts it down
        # to the two that approve. Both, because they answer different
        # questions -- "is this a verdict at all" and "is it an approving one".
        _state_check(
            "approval_decision", Verdict, "ck_scheduled_posts_approval_decision"
        ),
        # FR-13. One live schedule per variant: a variant that is queued, being
        # published, or already published cannot be queued again. Partial, so a
        # cancelled or dead-lettered attempt frees the variant for a genuine
        # retry, and so the index only carries rows the publisher cares about.
        sa.Index(
            "uq_live_schedule_per_variant",
            "variant_id",
            unique=True,
            postgresql_where=sa.text(f"state IN ({_OCCUPYING_SQL})"),
            sqlite_where=sa.text(f"state IN ({_OCCUPYING_SQL})"),
        ),
        # The publisher's claim query: due rows in a claimable state, oldest
        # first. Composite and in this order because the state is the equality
        # predicate and the time is the range one.
        sa.Index("ix_scheduled_post_due", "state", "scheduled_for"),
        # The second line of defence against a double publish: if a retry did
        # land a duplicate, the two rows cannot both record an external id.
        # Partial, because unpublished rows all have NULL there.
        sa.Index(
            "uq_scheduled_post_external_id",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
            sqlite_where=sa.text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()
    variant_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("post_variants.id", ondelete="RESTRICT"), nullable=False
    )

    # Half of the composite FK above. Denormalised on purpose: it is the price
    # of being able to state "this row's approval was an approval" in SQL.
    approval_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    approval_decision: Mapped[Verdict] = _state_column(Verdict, Verdict.APPROVED)

    # Which account, resolved against brand.yaml at publish time rather than
    # stored -- see the module docstring on why there is no accounts table.
    platform: Mapped[str] = mapped_column(sa.String(PLATFORM_LENGTH), nullable=False)

    # When it should go out. "Now" is a perfectly good value; the column exists
    # so FR-12 is true even then -- the worker publishes on its own schedule,
    # not as a continuation of the conversation that approved the post.
    scheduled_for: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )

    state: Mapped[ScheduleState] = _state_column(
        ScheduleState, ScheduleState.SCHEDULED
    )

    # FR-14's backoff and dead-letter split. `attempts` counts publish calls
    # made, so it increments before the call, not after a failure -- a process
    # killed mid-publish must not look like it never tried.
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )

    # Who holds the claim, and since when. `claimed_at` is what a reaper reads
    # to find a worker that died holding a row -- the one case where a row sits
    # in PUBLISHING with nobody publishing it.
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(sa.String(128))

    # What the platform gave back. For Facebook this is Graph's composite
    # "{page_id}_{post_id}", kept whole because that is what /{post-id} takes.
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    external_url: Mapped[str | None] = mapped_column(sa.Text)

    # The last failure, for the operator notification FR-14 owes them. A string,
    # not a code: what an operator needs is the sentence, and PublishFailed
    # already composes one that names the fbtrace_id.
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    variant: Mapped["PostVariant"] = relationship()

    def __repr__(self) -> str:
        return f"<ScheduledPost {self.id} {self.platform} {self.state}>"


# --- what happened after it went out ---------------------------------------
#
# Both tables below hang off ScheduledPost rather than PostVariant, because
# `external_id` is what a poller has in its hand: it reads a platform post id
# back from Graph and needs the row it belongs to. From there the path to the
# question actually being asked -- "which kinds of post work" (FR-4) -- runs
# scheduled_post -> variant -> draft.angle, which is why that column is indexed.
#
# Both also keep a `raw` payload alongside the modelled columns. That is what
# makes it safe to build these before the metrics workers exist: the platforms
# disagree about what an engagement even is (Facebook has six reaction types,
# YouTube has watch time, X has bookmarks), and a column nobody thought to add
# is a fact lost forever, while a JSONB blob is a fact you can model later.


class PostMetric(Base):
    """A snapshot of one published post's counters, at one moment. FR-16.

    A time series, not a running total, and that is the whole design. Engagement
    arrives in a curve -- most posts collect the bulk of it in the first hours --
    so a single mutable row of current counts would answer "how did it do" while
    destroying "how fast did it do it", which is the more useful signal when
    deciding what to post next.

    Every counter is nullable because no platform supplies all of them, and a
    zero would be a lie about a metric that was never reported.
    """

    __tablename__ = "post_metrics"
    __table_args__ = (
        # One snapshot per post per collection time; a poller that runs twice on
        # the same schedule tick updates rather than duplicating.
        sa.UniqueConstraint(
            "scheduled_post_id", "collected_at", name="uq_metric_post_collected"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()
    scheduled_post_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("scheduled_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # When we asked, not when anything happened. The counters are cumulative as
    # of this instant.
    collected_at: Mapped[datetime] = _created_at()

    impressions: Mapped[int | None] = mapped_column(sa.Integer)
    reach: Mapped[int | None] = mapped_column(sa.Integer)
    # Reactions of every kind summed. The per-type breakdown, where a platform
    # gives one, is in `raw` -- six nullable columns for Facebook's reaction set
    # would be six columns every other platform leaves null.
    reactions: Mapped[int | None] = mapped_column(sa.Integer)
    comments: Mapped[int | None] = mapped_column(sa.Integer)
    shares: Mapped[int | None] = mapped_column(sa.Integer)
    clicks: Mapped[int | None] = mapped_column(sa.Integer)
    saves: Mapped[int | None] = mapped_column(sa.Integer)
    video_views: Mapped[int | None] = mapped_column(sa.Integer)

    # The platform's own response, unmodelled. See the section comment above.
    raw: Mapped[dict] = mapped_column(
        sa.JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )

    def __repr__(self) -> str:
        return f"<PostMetric {self.scheduled_post_id} at {self.collected_at}>"


class PostEngagement(Base):
    """One thing one follower did: a comment, a reaction, a share.

    Rows, not counts -- `post_metrics` already holds the counts. What this adds
    is the content and the who, which is what makes a comment worth more than a
    +1 to a number when the agent is deciding what to write next.

    Append-only. Nothing here is authored by us and nothing here is edited by
    us; a comment the follower later deleted is recorded as having happened.
    """

    __tablename__ = "post_engagements"
    __table_args__ = (
        # Polling is repeated and overlapping -- the same comment comes back on
        # every run -- so the platform's own id is what makes collection
        # idempotent. Partial, because a platform that reports an anonymous
        # reaction with no id has nothing to dedupe on and the poller has to
        # handle that itself.
        sa.Index(
            "uq_engagement_external_id",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
            sqlite_where=sa.text("external_id IS NOT NULL"),
        ),
        # The read the agent actually makes: this post's engagement, newest first.
        sa.Index("ix_engagement_post_occurred", "scheduled_post_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    brand_slug: Mapped[str] = _brand_slug()
    scheduled_post_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("scheduled_posts.id", ondelete="CASCADE"), nullable=False
    )

    # Denormalised from the scheduled post so the dedupe index above can be
    # scoped without a join -- ids are only unique within a platform.
    platform: Mapped[str] = mapped_column(sa.String(PLATFORM_LENGTH), nullable=False)

    # `comment`, `reaction`, `share`. A string for the same reason `platform` is
    # one: these arrive from platform APIs that disagree about what exists, and
    # discovering a new kind should be a row someone reads, not a migration that
    # has to land before the data can be stored at all.
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    # `like`, `love`, `haha`... for a reaction; null for anything else.
    reaction_type: Mapped[str | None] = mapped_column(sa.String(32))

    # The platform's id for this engagement. Nullable: aggregate reaction counts
    # come back without one.
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    # The comment this replies to, when it is a reply. The platform's id, not
    # ours, so a thread can be reassembled even if we never stored the parent.
    parent_external_id: Mapped[str | None] = mapped_column(sa.String(128))

    # Who. Nullable throughout and often null in practice -- Facebook only
    # discloses a commenter's identity to apps with the right permissions, and
    # reactions are usually anonymous in aggregate. Store what is offered; never
    # require it.
    author_external_id: Mapped[str | None] = mapped_column(sa.String(128))
    author_name: Mapped[str | None] = mapped_column(sa.String(128))

    # The comment text. Null for a reaction or a share, which carry no words.
    content: Mapped[str | None] = mapped_column(sa.Text)

    # When the follower did it, per the platform. Nullable because not every
    # platform says -- which is why it is separate from `collected_at`, the only
    # timestamp we can always vouch for.
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    collected_at: Mapped[datetime] = _created_at()

    raw: Mapped[dict] = mapped_column(
        sa.JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )

    def __repr__(self) -> str:
        return f"<PostEngagement {self.kind} on {self.scheduled_post_id}>"
