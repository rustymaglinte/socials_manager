"""The post lifecycle: which state changes are allowed, and which are not.

SPECS 6.1 draws the lifecycle as one line, but there are two rows in it:

    DRAFT -> PENDING_APPROVAL -> APPROVED | SCHEDULED -> PUBLISHING -> PUBLISHED
                  |                 |     |                   |
              REJECTED          CANCELLED|           FAILED -> DEAD_LETTER
                                         ^
                              a draft ends here; a scheduled post begins

Split into two enums rather than one, because half the states are unreachable
for a draft and the other half unreachable for a scheduled post -- and a single
enum would make `draft.state = PUBLISHING` representable, which is the exact
class of mistake this module exists to prevent. The handoff between them is
`may_schedule`: it is the only place the two lifecycles touch.

Framework-free by contract (SPECS D1): stdlib only, no ORM, no database. These
are the rules; `app.store` is where rows obeying them live. Written this way so
the rules can be read and tested without a Postgres running -- and so the store
cannot quietly encode a different set of them in column defaults.

Transitions are enforced here, never in tools (SPECS 6.1).
"""

from enum import StrEnum
from typing import TypeVar

# A TypeVar rather than PEP 695's `def _advance[S: StrEnum]`, which is 3.12
# syntax: pyproject declares a 3.11 floor, and a SyntaxError is a poor way to
# find out that the floor was real.
S = TypeVar("S", bound=StrEnum)


class IllegalTransition(ValueError):
    """A state change the lifecycle does not allow.

    Raised rather than logged: every caller of `advance_*` is about to write a
    row, and a rejected transition means the caller's model of the world is
    wrong. Continuing would persist that wrongness.
    """


class DraftState(StrEnum):
    """Where a draft is between "the agent wrote it" and "a human ruled on it".

    Values are the lowercase strings stored in the database. Named separately
    from the members so a rename here is not a migration.
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class ScheduleState(StrEnum):
    """Where a post is between "approved" and "live, or given up on"."""

    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class Verdict(StrEnum):
    """What a human said about a draft.

    `timeout` is a verdict, not the absence of one: nobody looked, and that is a
    fact worth recording rather than a null to interpret later. Mirrors the
    `Decision` literal in the Slack transport's pending registry, which should
    come to import this rather than spell the four strings again.
    """

    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


# The two verdicts that let a post proceed. An edit is an approval -- the
# reviewer accepted the post, having rewritten it -- so both belong here.
#
# The store turns this set into a CHECK constraint, which is what makes "no post
# reaches a platform without an Approval row" (FR-8) true in SQL rather than
# true by convention: without it, an `approval_id` could point at a rejection.
APPROVING_VERDICTS = frozenset({Verdict.APPROVED, Verdict.EDITED})


_DRAFT_TRANSITIONS: dict[DraftState, frozenset[DraftState]] = {
    DraftState.DRAFT: frozenset({DraftState.PENDING_APPROVAL}),
    DraftState.PENDING_APPROVAL: frozenset({DraftState.APPROVED, DraftState.REJECTED}),
    # Terminal for the draft. An approved draft is not edited further; the
    # scheduled post takes over, and a change of mind is a cancellation over
    # there rather than a draft that quietly reopens under an existing approval.
    DraftState.APPROVED: frozenset(),
    # FR-11: a rejection with feedback sends the draft back for a rewrite. Not
    # terminal, which is why the revision cap lives in the caller (app.main's
    # MAX_REVISIONS) -- the lifecycle permits the loop, policy bounds it.
    DraftState.REJECTED: frozenset({DraftState.DRAFT}),
}

_SCHEDULE_TRANSITIONS: dict[ScheduleState, frozenset[ScheduleState]] = {
    ScheduleState.SCHEDULED: frozenset(
        {ScheduleState.PUBLISHING, ScheduleState.CANCELLED}
    ),
    # Deliberately no PUBLISHING -> SCHEDULED edge, and this is the single most
    # load-bearing omission in the file. A worker that dies mid-publish leaves a
    # row claimed, and the tempting recovery -- hand it back to the queue -- is
    # how you double-post: the adapter cannot tell a request that never landed
    # from one whose response was lost (app/platforms/facebook.py, `_send`), and
    # Graph has no idempotency key to settle it. So a stale claim goes to FAILED
    # and stops. Getting back out of FAILED is where something has to check
    # whether the post actually went up (FR-13).
    ScheduleState.PUBLISHING: frozenset({ScheduleState.PUBLISHED, ScheduleState.FAILED}),
    # FR-14: retry with backoff, dead-letter after N attempts. CANCELLED as well,
    # so an operator can stop a post that is still retrying without waiting for
    # it to exhaust its attempts.
    ScheduleState.FAILED: frozenset(
        {ScheduleState.SCHEDULED, ScheduleState.DEAD_LETTER, ScheduleState.CANCELLED}
    ),
    ScheduleState.PUBLISHED: frozenset(),
    ScheduleState.DEAD_LETTER: frozenset(),
    ScheduleState.CANCELLED: frozenset(),
}

TERMINAL_DRAFT_STATES = frozenset(
    state for state, allowed in _DRAFT_TRANSITIONS.items() if not allowed
)
TERMINAL_SCHEDULE_STATES = frozenset(
    state for state, allowed in _SCHEDULE_TRANSITIONS.items() if not allowed
)

# What the publisher's claim query looks for. Named rather than inlined there so
# the worker and the lifecycle cannot disagree about what "due" means.
CLAIMABLE_SCHEDULE_STATES = frozenset({ScheduleState.SCHEDULED})

# States in which a scheduled post still occupies its variant. The store makes
# this a partial unique index: one live schedule per variant, so a variant that
# already went out cannot be queued a second time (FR-13). PUBLISHED is in here
# on purpose -- a re-post is a new variant, not a second run at an old one.
OCCUPYING_SCHEDULE_STATES = frozenset(ScheduleState) - {
    ScheduleState.CANCELLED,
    ScheduleState.DEAD_LETTER,
}


def _advance(current: S, to: S, transitions: dict[S, frozenset[S]], what: str) -> S:
    """Check one transition against its table, or raise.

    Returns `to` so a caller can write `row.state = advance_draft(row.state, X)`
    and have the check be unskippable -- an assignment that does not go through
    here is visibly different from one that does.
    """
    allowed = transitions[current]
    if to not in allowed:
        # Say what was possible, not just what was not: the caller is usually a
        # worker whose next move depends on where the row actually is.
        options = ", ".join(sorted(state.value for state in allowed)) or "nothing"
        raise IllegalTransition(
            f"A {what} cannot go from {current.value} to {to.value}; "
            f"from {current.value} it can only go to {options}."
        )
    return to


def advance_draft(current: DraftState, to: DraftState) -> DraftState:
    """The next state of a draft, or IllegalTransition."""
    return _advance(current, to, _DRAFT_TRANSITIONS, "draft")


def advance_schedule(current: ScheduleState, to: ScheduleState) -> ScheduleState:
    """The next state of a scheduled post, or IllegalTransition."""
    return _advance(current, to, _SCHEDULE_TRANSITIONS, "scheduled post")


def may_schedule(draft: DraftState) -> bool:
    """Whether a draft in this state may have a post scheduled from it.

    The join between the two lifecycles, and the reason it is a function rather
    than a comparison written at each call site: it is C-1 in code form. The
    store enforces the same thing again with a foreign key, because a rule that
    only exists in Python is a rule the next process to touch the database does
    not know about.
    """
    return draft is DraftState.APPROVED
