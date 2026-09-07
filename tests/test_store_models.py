"""app.store.models -- the schema's architectural claims, checked against metadata.

No database. These are not tests of SQLAlchemy; they are tests of the three
promises SPECS and the README make about the schema, each of which is one
`nullable=True` away from being quietly false:

- every table is brand-scoped (SPECS 6)
- a scheduled post cannot exist without an *approving* verdict (C-1, FR-8, D2)
- a variant cannot be queued twice (FR-13)

A regression in any of them would otherwise surface as a cross-brand post or a
double publish, neither of which a unit test on a repository would catch.
"""

import pytest
import sqlalchemy as sa

from app.domain.states import (
    APPROVING_VERDICTS,
    OCCUPYING_SCHEDULE_STATES,
    DraftState,
    ScheduleState,
    Verdict,
)
from app.store import (
    Approval,
    Base,
    Brand,
    Draft,
    PostEngagement,
    PostMedia,
    PostMetric,
    PostVariant,
    ScheduledPost,
)

# `brands` is the anchor the others point at, so it holds a slug rather than a
# brand_slug. Everything else is scoped -- including the engagement tables,
# whose rows are written by a poller rather than by the agent and would be the
# easy ones to forget.
SCOPED_TABLES = [
    Draft,
    PostVariant,
    PostMedia,
    Approval,
    ScheduledPost,
    PostMetric,
    PostEngagement,
]


def test_no_table_escapes_the_scoping_rule():
    """Guards the list above: a new model added without a brand_slug would
    otherwise be tested by nothing, because nobody thought to list it."""
    listed = {model.__tablename__ for model in SCOPED_TABLES} | {Brand.__tablename__}
    assert listed == set(Base.metadata.tables)


def test_every_table_is_brand_scoped():
    """SPECS 6: "Every table carries brand_id NOT NULL"."""
    for model in SCOPED_TABLES:
        column = model.__table__.columns["brand_slug"]
        assert not column.nullable, f"{model.__tablename__}.brand_slug is nullable"
        assert column.foreign_keys, f"{model.__tablename__}.brand_slug has no FK"


def test_brand_scoping_is_indexed():
    """Every repository query filters on it, so the index is not speculative."""
    for model in SCOPED_TABLES:
        assert model.__table__.columns["brand_slug"].index


def test_the_only_brand_attribute_stored_is_its_identity():
    """brand.yaml is the source of truth for tier, policy and voice.

    A second copy here is a second place for them to disagree, and the loader
    already warns about placeholders in the first.
    """
    assert set(Brand.__table__.columns.keys()) == {"slug", "created_at"}


def test_a_scheduled_post_requires_an_approval():
    """D2: approval enforced by a foreign key, not by prompt compliance."""
    columns = ScheduledPost.__table__.columns
    assert not columns["approval_id"].nullable
    assert not columns["approval_decision"].nullable


def test_the_approval_foreign_key_carries_the_decision():
    """The plain-FK version of this would accept a rejection row.

    The composite key onto (id, decision) is what lets the CHECK below actually
    test something -- without it there is nothing on this table to check.
    """
    composite = [
        constraint
        for constraint in ScheduledPost.__table__.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
        and len(constraint.elements) == 2
    ]
    assert composite, "scheduled_posts has no composite FK onto approvals"

    referenced = {element.target_fullname for element in composite[0].elements}
    assert referenced == {"approvals.id", "approvals.decision"}


def test_only_an_approving_verdict_can_back_a_scheduled_post():
    """C-1, in SQL. The CHECK must name every approving verdict and no other.

    Found by name, because the column carries two CHECKs: the enum's own, which
    admits all four verdicts, and this one, which narrows them to the two that
    approve. Matching on the clause text would find both.
    """
    checks = [
        constraint
        for constraint in ScheduledPost.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and constraint.name == "ck_scheduled_post_actually_approved"
    ]
    assert len(checks) == 1, "the approving-verdict CHECK is missing"

    clause = str(checks[0].sqltext)
    for verdict in APPROVING_VERDICTS:
        assert f"'{verdict.value}'" in clause
    for verdict in set(Verdict) - APPROVING_VERDICTS:
        assert f"'{verdict.value}'" not in clause, (
            f"{verdict.value} would pass the approval gate"
        )


def test_the_approvals_table_can_be_referenced_by_the_composite_key():
    """Postgres needs a unique constraint on the referenced pair; (id) alone
    would not let the decision travel with the id."""
    pairs = {
        tuple(constraint.columns.keys())
        for constraint in Approval.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("id", "decision") in pairs


def test_one_live_schedule_per_variant():
    """FR-13. The adapter cannot be idempotent, so the index has to be."""
    index = _index(ScheduledPost, "uq_live_schedule_per_variant")
    assert index.unique
    assert list(index.columns.keys()) == ["variant_id"]

    predicate = str(index.dialect_options["postgresql"]["where"])
    for state in OCCUPYING_SCHEDULE_STATES:
        assert f"'{state.value}'" in predicate
    for state in set(ScheduleState) - OCCUPYING_SCHEDULE_STATES:
        assert f"'{state.value}'" not in predicate


def test_a_platform_cannot_record_the_same_post_twice():
    """The second line of defence: if a retry did duplicate, only one row keeps
    the external id and the other fails to commit."""
    index = _index(ScheduledPost, "uq_scheduled_post_external_id")
    assert index.unique
    assert list(index.columns.keys()) == ["platform", "external_id"]


def test_one_variant_per_platform_per_draft():
    """The structural form of app.main's in-memory `submitted` set."""
    pairs = {
        tuple(constraint.columns.keys())
        for constraint in PostVariant.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("draft_id", "platform") in pairs


def test_the_publisher_has_an_index_to_claim_on():
    index = _index(ScheduledPost, "ix_scheduled_post_due")
    assert list(index.columns.keys()) == ["state", "scheduled_for"]


def test_state_columns_store_the_domain_values_not_the_member_names():
    """SQLAlchemy's default is to store NAMES. A column full of
    PENDING_APPROVAL would not match anything app.domain.states looks for.
    """
    cases = [
        (Draft, "state", DraftState),
        (ScheduledPost, "state", ScheduleState),
        (Approval, "decision", Verdict),
    ]
    for model, column_name, enum in cases:
        stored = set(model.__table__.columns[column_name].type.enums)
        assert stored == {member.value for member in enum}


def test_engagement_attributes_back_to_the_angle_that_produced_it():
    """FR-4 and FR-16: "which kinds of post work" is a group-by on draft.angle,
    reached from an engagement row. Every hop in that path has to exist."""
    assert PostEngagement.__table__.columns["scheduled_post_id"].foreign_keys
    assert PostMetric.__table__.columns["scheduled_post_id"].foreign_keys
    assert ScheduledPost.__table__.columns["variant_id"].foreign_keys
    assert PostVariant.__table__.columns["draft_id"].foreign_keys
    # The group-by column, and the reason it carries an index.
    assert Draft.__table__.columns["angle"].index


def test_polling_the_same_engagement_twice_cannot_duplicate_it():
    """Collection runs overlap by design -- the same comment comes back every
    poll -- so the platform's own id is what makes the poller idempotent."""
    index = _index(PostEngagement, "uq_engagement_external_id")
    assert index.unique
    assert list(index.columns.keys()) == ["platform", "external_id"]


def test_metrics_are_a_time_series_not_a_running_total():
    """A mutable row of current counts would answer "how did it do" and destroy
    "how fast did it do it", which is the signal worth having."""
    pairs = {
        tuple(constraint.columns.keys())
        for constraint in PostMetric.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("scheduled_post_id", "collected_at") in pairs


def test_no_engagement_counter_is_mandatory():
    """No platform reports all of them, and a zero would be a lie about a metric
    that was never returned."""
    counters = ["impressions", "reach", "reactions", "comments", "shares", "clicks"]
    for name in counters:
        assert PostMetric.__table__.columns[name].nullable


def test_unmodelled_platform_data_is_kept():
    """The reason these tables are safe to build before the pollers exist: a
    column nobody thought to add loses the fact forever, JSONB does not."""
    for model in (PostMetric, PostEngagement):
        assert not model.__table__.columns["raw"].nullable


@pytest.mark.parametrize(
    ("model", "column", "enum", "name"),
    [
        (Draft, "state", DraftState, "ck_drafts_state"),
        (Approval, "decision", Verdict, "ck_approvals_decision"),
        (ScheduledPost, "state", ScheduleState, "ck_scheduled_posts_state"),
        (
            ScheduledPost,
            "approval_decision",
            Verdict,
            "ck_scheduled_posts_approval_decision",
        ),
    ],
)
def test_every_state_column_has_a_check_alembic_can_see(model, column, enum, name):
    """Declared explicitly rather than generated by `sa.Enum(create_constraint=True)`.

    The generated form is *type-bound*, which Alembic's comparison does not
    recognise as part of the metadata: it reports four constraints in the
    database matching nothing in the models and offers a migration that drops
    the validation from every lifecycle column. Declared this way they compare
    cleanly -- and adding a state to app.domain.states now registers as real
    drift, so the migration must land before a row can carry the new value.
    """
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert name in checks, f"{model.__tablename__}.{column} has no named CHECK"
    for member in enum:
        assert f"'{member.value}'" in checks[name]


def test_timestamps_are_timezone_aware():
    """A Manila brand on a UTC host: a naive timestamp is a calendar bug (FR-17)."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.DateTime):
                assert column.type.timezone, f"{table.name}.{column.name} is naive"


def _index(model, name: str) -> sa.Index:
    for index in model.__table__.indexes:
        if index.name == name:
            return index
    raise AssertionError(f"{model.__tablename__} has no index {name!r}")
