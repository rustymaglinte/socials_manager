"""Resolving a brand to the secrets it publishes with.

The join that `app.platforms` is forbidden to make. An adapter is handed a page
id and a token and never learns whose Page it is ("Adapters are brand-blind" in
pyproject.toml); somebody above has to do the resolving, and this is that
somebody. `scripts/fb_publish.py` has been making the same join by hand since
the adapter landed -- this is where it belongs once a worker needs it too, so
there is one place that knows how a brand becomes a credential rather than two
that can disagree.

Environment variables for now. README describes `credentials/` as an encrypted
token vault with refresh schedules, and LinkedIn's 60-day expiry (SPECS 7.2)
will force that; the shape here -- ask for a brand and a platform, get a
credential or a clear error -- is what the vault will implement, so callers do
not change when it arrives.

Never logs, prints, or puts a token in an exception message.
"""

import os
from dataclasses import dataclass

from app.domain.brand.context import Account, BrandContext

# How each platform's token is named in the environment. Derived from the slug
# rather than mapped brand-by-brand, so a second brand's Page needs a line in
# .env and no code at all -- the same shape as SLACK_<SLUG>_CHANNEL_ID.
#
# `FB_PAGE_TOKEN_<SLUG>` is the spelling scripts/fb_publish.py established and
# that operators already have in .env; changing it here would silently break a
# working setup to gain nothing.
_TOKEN_PREFIX = {
    "facebook": "FB_PAGE_TOKEN",
    "linkedin": "LINKEDIN_TOKEN",
    "x": "X_TOKEN",
    "youtube": "YOUTUBE_TOKEN",
}


class CredentialsMissing(LookupError):
    """This brand cannot publish to this platform yet, and why.

    A LookupError rather than a SystemExit: a CLI can turn this into an exit
    with a message, but a worker publishing a batch must be able to fail one
    post and carry on with the rest.

    Every message names the file or variable to fix. These are configuration
    mistakes an operator makes once per account, and the useful answer is
    always "open this and set that".
    """


@dataclass(frozen=True)
class PlatformCredentials:
    """What an adapter needs, and nothing that identifies the brand.

    `external_id` is the Page id, channel id or handle -- whatever the platform
    calls the thing being posted to. The brand's slug is deliberately absent:
    what gets handed down to `app.platforms` should not be able to leak whose
    account it is, because that is the property making cross-brand posting
    structurally impossible (SPECS D3) rather than merely unlikely.
    """

    external_id: str
    token: str


def token_env_var(slug: str, platform: str) -> str:
    """`pinoysing`, `facebook` -> `FB_PAGE_TOKEN_PINOYSING`."""
    prefix = _TOKEN_PREFIX.get(platform)
    if prefix is None:
        raise CredentialsMissing(
            f"No token convention for platform {platform!r}. Add one to "
            f"_TOKEN_PREFIX in app/credentials/tokens.py."
        )
    return f"{prefix}_{slug.upper()}"


def account_for(brand: BrandContext, platform: str) -> Account:
    """This brand's account on `platform`, or say what is wrong with it.

    Three different failures, three different fixes, so they are separated here
    rather than collapsed into "not configured".
    """
    account = next(
        (a for a in brand.accounts if a.platform == platform), None
    )
    if account is None:
        raise CredentialsMissing(
            f"{brand.slug} has no {platform} account in "
            f"brands/{brand.slug}/brand.yaml"
        )
    if not account.enabled:
        raise CredentialsMissing(
            f"{brand.slug}'s {platform} account is disabled in "
            f"brands/{brand.slug}/brand.yaml"
        )
    # `configured` is the placeholder check the adapter deliberately does not
    # do: knowing what a TODO looks like is the domain's business.
    if not account.configured:
        raise CredentialsMissing(
            f"The {platform} id for {brand.slug} is still a placeholder in "
            f"brands/{brand.slug}/brand.yaml (SPECS Q2)"
        )
    return account


def credentials_for(brand: BrandContext, platform: str) -> PlatformCredentials:
    """(id, token) for one brand's account on one platform.

    Raises CredentialsMissing with something actionable if either half is
    absent. The token is read fresh on every call rather than cached, so
    rotating one in .env takes a worker restart at most -- and once the vault
    lands, no restart at all.
    """
    account = account_for(brand, platform)

    variable = token_env_var(brand.slug, platform)
    token = os.getenv(variable)
    if not token:
        raise CredentialsMissing(f"Set {variable} in .env")

    # `configured` already ruled out None and the placeholder; binding it to a
    # local is what narrows the Optional for a type checker as well as a reader.
    # Raised rather than asserted: `python -O` strips assertions, and the thing
    # that must never happen is a None reaching an adapter as the string "None"
    # and being posted to whatever Page that resolves to.
    external_id = account.external_id
    if external_id is None:  # pragma: no cover - account_for rules this out
        raise CredentialsMissing(
            f"{brand.slug} has no {platform} id despite passing its checks"
        )

    return PlatformCredentials(external_id=external_id, token=token)
