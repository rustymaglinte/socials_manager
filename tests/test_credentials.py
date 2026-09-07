"""app.credentials -- the brand -> secret join the adapters are forbidden to make.

Every failure here is a configuration mistake an operator makes once per
account, so the tests are mostly about whether the message names the file or
variable to open.
"""

import pytest

from app.credentials import (
    CredentialsMissing,
    account_for,
    credentials_for,
    token_env_var,
)
from app.domain.brand import Account
from tests.conftest import make_brand


@pytest.mark.parametrize(
    ("slug", "platform", "expected"),
    [
        ("pinoysing", "facebook", "FB_PAGE_TOKEN_PINOYSING"),
        ("derekt", "facebook", "FB_PAGE_TOKEN_DEREKT"),
        ("personal", "linkedin", "LINKEDIN_TOKEN_PERSONAL"),
    ],
)
def test_the_variable_name_is_derived_from_the_slug(slug, platform, expected):
    """A second brand's Page needs a line in .env and no code at all -- the same
    shape as SLACK_<SLUG>_CHANNEL_ID."""
    assert token_env_var(slug, platform) == expected


def test_the_facebook_variable_matches_what_operators_already_have():
    """scripts/fb_publish.py established this spelling and .env files use it.
    Renaming it here would break a working setup to gain nothing."""
    assert token_env_var("pinoysing", "facebook") == "FB_PAGE_TOKEN_PINOYSING"


def test_an_unknown_platform_says_where_to_add_it():
    with pytest.raises(CredentialsMissing, match="_TOKEN_PREFIX"):
        token_env_var("pinoysing", "tiktok")


# --- the three ways an account is not usable, which need three fixes --------


def test_a_platform_the_brand_does_not_have():
    brand = make_brand(accounts=())
    with pytest.raises(CredentialsMissing, match="no facebook account"):
        account_for(brand, "facebook")


def test_a_disabled_account_is_not_a_missing_one():
    brand = make_brand(
        accounts=(Account(platform="facebook", enabled=False, external_id="67890"),)
    )
    with pytest.raises(CredentialsMissing, match="disabled"):
        account_for(brand, "facebook")


def test_an_id_still_at_its_placeholder():
    """brand.yaml ships as a template full of TODOs (SPECS Q2). Draftable, but
    not publishable, and the error should say which."""
    brand = make_brand(
        accounts=(Account(platform="x", enabled=True, external_id="TODO"),)
    )
    with pytest.raises(CredentialsMissing, match="placeholder"):
        account_for(brand, "x")


# --- the token ------------------------------------------------------------


def test_a_missing_token_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv("FB_PAGE_TOKEN_DEREKT", raising=False)
    with pytest.raises(CredentialsMissing, match="Set FB_PAGE_TOKEN_DEREKT in .env"):
        credentials_for(make_brand(), "facebook")


def test_a_configured_account_resolves(monkeypatch):
    monkeypatch.setenv("FB_PAGE_TOKEN_DEREKT", "not-a-real-token")
    page = credentials_for(make_brand(), "facebook")
    assert page.external_id == "67890"
    assert page.token == "not-a-real-token"


def test_what_reaches_the_adapter_cannot_identify_the_brand(monkeypatch):
    """SPECS D3. The adapter is handed an id and a token and never learns whose
    Page it is -- that is what makes cross-brand posting structurally
    impossible rather than merely discouraged, so the credential carries no
    slug for an adapter to read."""
    monkeypatch.setenv("FB_PAGE_TOKEN_DEREKT", "not-a-real-token")
    page = credentials_for(make_brand(), "facebook")
    assert not hasattr(page, "slug")
    assert "derekt" not in repr(page).lower()


def test_the_token_is_read_fresh_every_time(monkeypatch):
    """Rotating a token in .env should cost a restart at most, never a code
    change -- and once the vault lands (SPECS 7.2), not even that."""
    brand = make_brand()
    monkeypatch.setenv("FB_PAGE_TOKEN_DEREKT", "first")
    assert credentials_for(brand, "facebook").token == "first"
    monkeypatch.setenv("FB_PAGE_TOKEN_DEREKT", "second")
    assert credentials_for(brand, "facebook").token == "second"
