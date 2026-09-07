"""Where a draft goes when a verdict lands on one of its variants.

No database. `next_draft_state` is the one piece of the draft repository that is
pure arithmetic on the lifecycle, and it is worth testing on its own because
both of the paths it adds exist only to reconcile a mismatch: approvals are per
variant, draft state is per draft. Everything else in `drafts.py` is a query and
is exercised against a real Postgres in test_store_repositories.py.
"""

import pytest

from app.domain.states import DraftState, IllegalTransition, advance_draft
from app.store.repositories import next_draft_state


def test_a_new_draft_enters_review():
    assert (
        next_draft_state(DraftState.DRAFT, DraftState.PENDING_APPROVAL)
        is DraftState.PENDING_APPROVAL
    )


def test_a_reviewed_draft_settles_either_way():
    assert (
        next_draft_state(DraftState.PENDING_APPROVAL, DraftState.APPROVED)
        is DraftState.APPROVED
    )
    assert (
        next_draft_state(DraftState.PENDING_APPROVAL, DraftState.REJECTED)
        is DraftState.REJECTED
    )


def test_a_rejected_draft_can_go_back_for_a_rewrite():
    """FR-11, and the reason this function is not just `advance_draft`: the
    lifecycle spells the return trip REJECTED -> DRAFT -> PENDING_APPROVAL, so a
    single call to advance_draft would refuse the revision loop it permits."""
    with pytest.raises(IllegalTransition):
        advance_draft(DraftState.REJECTED, DraftState.PENDING_APPROVAL)

    assert (
        next_draft_state(DraftState.REJECTED, DraftState.PENDING_APPROVAL)
        is DraftState.PENDING_APPROVAL
    )


def test_a_draft_that_has_cleared_stays_cleared():
    """The second platform of a four-platform concept is reviewed after the
    first one was approved. That review is of a variant, not of the concept, so
    it must not raise on an edge out of a terminal state -- and must not undo
    the approval either."""
    for verdict in (
        DraftState.PENDING_APPROVAL,
        DraftState.APPROVED,
        DraftState.REJECTED,
    ):
        assert next_draft_state(DraftState.APPROVED, verdict) is DraftState.APPROVED


def test_repeating_a_state_is_a_no_op():
    for state in DraftState:
        assert next_draft_state(state, state) is state


def test_an_impossible_move_still_raises():
    """The tolerance above is specific, not general: a draft cannot be approved
    without having been reviewed."""
    with pytest.raises(IllegalTransition):
        next_draft_state(DraftState.DRAFT, DraftState.APPROVED)
