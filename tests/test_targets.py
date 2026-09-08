"""app.agent.targets -- what a run is allowed to aim at.

The bug behind this module: briefed with three platforms and told it could ask
one question, the model asked "Facebook, X, or YouTube?" and the run ended.
Nothing it writes outside a tool call reaches a human, so that question was
unanswerable by construction -- which makes "which platforms?" a decision that
has to be made before the model is invoked, like the brand (SPECS D3).

The two bars a target clears fail for different reasons and want different
fixes, so the tests below check that the message says which one was missed.
"""

import pytest

from app.agent.targets import NoTargetPlatform, require_targets, target_platforms
from app.domain.brand import PLACEHOLDER, Account
from tests.conftest import make_brand


def brand_with(*accounts) -> object:
    return make_brand(accounts=tuple(accounts))


def test_a_configured_enabled_account_with_an_adapter_is_a_target():
    brand = brand_with(Account(platform="facebook", enabled=True, external_id="67890"))

    assert target_platforms(brand) == ("facebook",)


def test_a_disabled_account_is_not_a_target():
    brand = brand_with(Account(platform="facebook", enabled=False, external_id="67890"))

    assert target_platforms(brand) == ()


def test_an_account_still_on_its_placeholder_is_not_a_target():
    """`configured` calls this draftable but not publishable. It is not
    draftable either: the reviewer would approve a post the worker dead-letters."""
    brand = brand_with(
        Account(platform="facebook", enabled=True, external_id=PLACEHOLDER)
    )

    assert target_platforms(brand) == ()


def test_a_platform_with_no_publisher_adapter_is_not_a_target():
    """linkedin is a real, configured, enabled account on the derekt fixture.
    There is no adapter for it, so a post there has nowhere to go."""
    brand = brand_with(Account(platform="linkedin", enabled=True, external_id="12345"))

    assert target_platforms(brand) == ()


def test_targets_keep_the_order_brand_yaml_declared_them_in():
    brand = brand_with(
        Account(platform="linkedin", enabled=True, external_id="12345"),
        Account(platform="facebook", enabled=True, external_id="67890"),
    )

    assert target_platforms(brand) == ("facebook",)


def test_require_targets_returns_the_list_when_there_is_one():
    assert require_targets(make_brand()) == ("facebook",)


def test_a_brand_with_no_accounts_at_all_says_so():
    with pytest.raises(NoTargetPlatform) as raised:
        require_targets(make_brand(slug="empty", accounts=()))

    assert "no enabled account in brands/empty/brand.yaml" in str(raised.value)


def test_the_message_names_the_placeholder_that_has_to_be_filled_in():
    """The fix is a line of yaml, so the message has to name the line."""
    brand = make_brand(
        slug="derekt",
        accounts=(Account(platform="facebook", enabled=True, external_id=PLACEHOLDER),),
    )

    with pytest.raises(NoTargetPlatform) as raised:
        require_targets(brand)

    message = str(raised.value)
    assert "still a placeholder in brands/derekt/brand.yaml: facebook" in message
    assert "can publish to facebook" in message


def test_the_message_distinguishes_a_missing_adapter_from_a_missing_id():
    """Two different fixes: fill in the yaml, or write an adapter."""
    brand = make_brand(
        accounts=(
            Account(platform="linkedin", enabled=True, external_id="12345"),
            Account(platform="youtube", enabled=True, external_id=PLACEHOLDER),
        )
    )

    with pytest.raises(NoTargetPlatform) as raised:
        require_targets(brand)

    message = str(raised.value)
    assert "still a placeholder" in message and "youtube" in message
    assert "no publisher adapter exists: linkedin" in message
