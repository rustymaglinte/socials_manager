"""Which platforms a run drafts for.

Decided here, before the model is invoked, for the same reason the brand is
(SPECS D3): there is nobody for the model to ask. Nothing it writes outside a
tool call reaches a human -- `app.main.run` only loops while the middleware
raises an interrupt, so an ordinary assistant turn ends the run -- and a run
that stopped to ask "Facebook, X, or YouTube?" is a run that drafted nothing and
cannot be answered.

A target has to clear two bars, and they fail for different reasons:

- The brand must have the account enabled *and* out of its TODO state
  (`publishable_accounts`). An id that is still a placeholder means the yaml has
  not been filled in.
- A publisher adapter must exist for the platform (`PUBLISHABLE_PLATFORMS`).
  Drafting for one that has none produces an approval a human spends attention
  on, and then a dead-lettered row: the worker fails it as not retryable.

Both are configuration facts, so `require_targets` names which bar was missed
rather than reporting "no platforms".
"""

from app.domain.brand import BrandContext
from app.platforms import PUBLISHABLE_PLATFORMS


class NoTargetPlatform(RuntimeError):
    """The brand has nowhere a drafted post could go. The message says why."""


def target_platforms(brand: BrandContext) -> tuple[str, ...]:
    """Every platform this brand can be briefed for, in brand.yaml's order.

    Possibly empty -- rendering the prompt must not raise. `require_targets` is
    the one that refuses.
    """
    return tuple(
        account.platform
        for account in brand.publishable_accounts
        if account.platform in PUBLISHABLE_PLATFORMS
    )


def require_targets(brand: BrandContext) -> tuple[str, ...]:
    """The same list, but refusing to start a run that has nothing to aim at.

    Raised rather than logged: a run with no target would burn a model call to
    produce a draft with no home, and the failure is a line of yaml away from
    fixed. `app.transports.slack_approval.handlers` turns this into a message in
    the channel the mention came from, so the person who asked sees the reason.
    """
    targets = target_platforms(brand)
    if not targets:
        raise NoTargetPlatform(_diagnosis(brand))
    return targets


def _diagnosis(brand: BrandContext) -> str:
    """Why this brand has no target, in terms of the file that fixes it."""
    where = f"brands/{brand.slug}/brand.yaml"
    enabled = brand.enabled_accounts

    if not enabled:
        return (
            f"{brand.slug} has no enabled account in {where}, so a draft would "
            f"have nowhere to go. Nothing was drafted."
        )

    reasons = []
    placeholders = [a.platform for a in enabled if not a.configured]
    if placeholders:
        reasons.append(
            f"still a placeholder in {where}: {', '.join(placeholders)}"
        )

    unsupported = [
        a.platform
        for a in brand.publishable_accounts
        if a.platform not in PUBLISHABLE_PLATFORMS
    ]
    if unsupported:
        reasons.append(f"no publisher adapter exists: {', '.join(unsupported)}")

    return (
        f"{brand.slug} has no platform a post could reach, so nothing was "
        f"drafted ({'; '.join(reasons)}). This app can publish to "
        f"{', '.join(sorted(PUBLISHABLE_PLATFORMS))}."
    )
