"""app.domain.states -- the lifecycle rules, tested without a database.

The two tests worth reading before the rest are
`test_a_claimed_post_cannot_be_handed_back_to_the_queue` and
`test_a_draft_state_is_never_a_schedule_state`: the first is FR-13's guarantee,
the second is the reason there are two enums rather than one.
"""

import pytest

from app.domain.states import (
    APPROVING_VERDICTS,
    CLAIMABLE_SCHEDULE_STATES,
    OCCUPYING_SCHEDULE_STATES,
    TERMINAL_DRAFT_STATES,
    TERMINAL_SCHEDULE_STATES,
    DraftState,
    IllegalTransition,
    ScheduleState,
    Verdict,
    advance_draft,
    advance_schedule,
    may_schedule,
)


@pytest.mark.parametrize("current", list(DraftState))
@pytest.mark.parametrize("to", list(DraftState))
def test_every_draft_pair_is_answered(current, to):
    """A state missing from the transition table raises KeyError, not
    IllegalTransition -- which would crash a worker instead of telling it no."""
    try:
        advance_draft(current, to)
    except IllegalTransition:
        pass


@pytest.mark.parametrize("current", list(ScheduleState))
@pytest.mark.parametrize("to", list(ScheduleState))
def test_every_schedule_pair_is_answered(current, to):
    try:
        advance_schedule(current, to)
    except IllegalTransition:
        pass


# --- the happy paths -------------------------------------------------------


def test_the_drafting_path():
    state = advance_draft(DraftState.DRAFT, DraftState.PENDING_APPROVAL)
    assert advance_draft(state, DraftState.APPROVED) is DraftState.APPROVED


def test_the_publishing_path():
    state = advance_schedule(ScheduleState.SCHEDULED, ScheduleState.PUBLISHING)
    assert advance_schedule(state, ScheduleState.PUBLISHED) is ScheduleState.PUBLISHED


def test_a_rejection_sends_the_draft_back_for_a_rewrite():
    """FR-11. The lifecycle permits the loop; app.main's MAX_REVISIONS bounds it."""
    state = advance_draft(DraftState.PENDING_APPROVAL, DraftState.REJECTED)
    assert advance_draft(state, DraftState.DRAFT) is DraftState.DRAFT


def test_a_failure_retries_and_then_dead_letters():
    """FR-14: backoff, then dead-letter after N attempts."""
    failed = advance_schedule(ScheduleState.PUBLISHING, ScheduleState.FAILED)
    assert advance_schedule(failed, ScheduleState.SCHEDULED) is ScheduleState.SCHEDULED
    assert (
        advance_schedule(failed, ScheduleState.DEAD_LETTER) is ScheduleState.DEAD_LETTER
    )


# --- the refusals that matter ----------------------------------------------


def test_a_claimed_post_cannot_be_handed_back_to_the_queue():
    """FR-13, and the single most important rule in the module.

    A worker that dies mid-publish leaves a row in PUBLISHING. Returning it to
    SCHEDULED is the obvious recovery and it is how you double-post: nothing can
    tell a request that never landed from one whose response was lost. The only
    way out is FAILED, where something has to check before retrying.
    """
    with pytest.raises(IllegalTransition):
        advance_schedule(ScheduleState.PUBLISHING, ScheduleState.SCHEDULED)


def test_a_draft_cannot_skip_the_approval_gate():
    """C-1: no auto-publish tier, not even by state assignment."""
    with pytest.raises(IllegalTransition):
        advance_draft(DraftState.DRAFT, DraftState.APPROVED)


def test_an_approved_draft_does_not_reopen():
    """Terminal on the draft side: once approved, a change of mind is a
    cancellation on the schedule, not a draft quietly editable under an
    approval a human already gave."""
    assert TERMINAL_DRAFT_STATES == {DraftState.APPROVED}
    for target in DraftState:
        with pytest.raises(IllegalTransition):
            advance_draft(DraftState.APPROVED, target)


def test_a_published_post_is_the_end_of_it():
    for state in TERMINAL_SCHEDULE_STATES:
        for target in ScheduleState:
            with pytest.raises(IllegalTransition):
                advance_schedule(state, target)


def test_a_state_cannot_advance_to_itself():
    """A no-op transition is a double-claim or a lost update, never an intent."""
    with pytest.raises(IllegalTransition):
        advance_schedule(ScheduleState.PUBLISHING, ScheduleState.PUBLISHING)


def test_the_error_says_what_was_possible():
    """The caller is usually a worker deciding what to do next."""
    with pytest.raises(IllegalTransition, match="failed, published"):
        advance_schedule(ScheduleState.PUBLISHING, ScheduleState.CANCELLED)


# --- the join between the two lifecycles -----------------------------------


@pytest.mark.parametrize("state", list(DraftState))
def test_only_an_approved_draft_may_be_scheduled(state):
    assert may_schedule(state) is (state is DraftState.APPROVED)


def test_a_draft_state_is_never_a_schedule_state():
    """The reason for two enums: no value is legal in both columns.

    Both are StrEnums, so a single flat enum would have let `draft.state =
    "publishing"` through every type check in the codebase.
    """
    assert not {state.value for state in DraftState} & {
        state.value for state in ScheduleState
    }


# --- the sets the store and the worker read --------------------------------


def test_approving_verdicts_are_the_two_that_let_a_post_through():
    assert APPROVING_VERDICTS == {Verdict.APPROVED, Verdict.EDITED}
    assert Verdict.REJECTED not in APPROVING_VERDICTS
    assert Verdict.TIMEOUT not in APPROVING_VERDICTS


def test_a_published_variant_still_occupies_its_slot():
    """FR-13 at the schema level: PUBLISHED must be in the occupying set, or the
    partial unique index would let an already-published variant be queued again."""
    assert ScheduleState.PUBLISHED in OCCUPYING_SCHEDULE_STATES
    assert ScheduleState.PUBLISHING in OCCUPYING_SCHEDULE_STATES
    assert ScheduleState.CANCELLED not in OCCUPYING_SCHEDULE_STATES
    assert ScheduleState.DEAD_LETTER not in OCCUPYING_SCHEDULE_STATES


def test_only_scheduled_rows_are_claimable():
    """Anything else in the claim query would race the worker holding the row."""
    assert CLAIMABLE_SCHEDULE_STATES == {ScheduleState.SCHEDULED}
