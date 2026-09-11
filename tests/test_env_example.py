""".env.example -- the list a deployment is filled in from.

This file exists because the README's list had drifted from the code's, and the
drift was invisible: TAVILY_API_KEY was read at import by
`app.agent.tools.web_search` and documented nowhere, while ANTHROPIC_API_KEY,
CREDENTIAL_ENCRYPTION_KEY, LLM_CHAT_MODEL and half a dozen OAuth client ids were
documented and read by nothing. Somebody filling in a Railway environment from
that list would have set eight variables that do nothing and missed the one that
stops the agent from starting.

So the list is asserted against the source rather than maintained by hand. The
scan is deliberately crude -- a regex over `os.getenv`, plus pydantic's own
field names, plus the two families that are built per brand -- because a crude
check that runs is worth more here than a precise one nobody maintains. It is
allowed to be over-eager: documenting a variable the code stopped reading costs
a line, while missing one costs a deploy.
"""

import re
from pathlib import Path

import pytest

from app.config import Settings
from app.credentials.tokens import token_env_var
from app.domain.brand import all_brands
from app.platforms import PUBLISHABLE_PLATFORMS
from app.transports.slack_approval.client import channel_env_var

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# `os.getenv("NAME")` with no second argument. A default means the code runs
# without it, so it is documentation rather than a requirement.
_REQUIRED_GETENV = re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']\s*\)""")


def _documented() -> set[str]:
    """Every variable named at the start of a line in .env.example.

    Commented lines count: `# X_TOKEN_PINOYSING=` documents a variable that is
    optional today, which is the point of listing it.
    """
    if not ENV_EXAMPLE.is_file():
        return set()

    names = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if match:
            names.add(match.group(1))
    return names


def _required_by_the_code() -> dict[str, str]:
    """name -> where it is read, for every variable with no fallback."""
    required: dict[str, str] = {}

    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in _REQUIRED_GETENV.findall(source):
            required.setdefault(name, str(path.relative_to(REPO_ROOT)))

    # pydantic reads these by field name; they never appear as `os.getenv`.
    for name, field in Settings.model_fields.items():
        if field.is_required():
            required.setdefault(name.upper(), "app/config.py")

    # Built per brand from an f-string, so there is no literal to find. These
    # are the two families that multiply as brands are added.
    for brand in all_brands():
        # Every brand directory, without exception: `start_listener` refuses to
        # boot unless all of them are routed, including brands nothing drafts for.
        required.setdefault(channel_env_var(brand.slug), "app/transports/.../client.py")

        # Tokens are narrower, and the narrowing is not a convenience -- it is
        # what the code does. `credentials_for` is only ever reached for an
        # account that is enabled, has a real id rather than a TODO
        # (`publishable_accounts`), and whose platform has a publisher adapter
        # (`PUBLISHABLE_PLATFORMS`). Anything outside that set is refused before
        # the token is looked up, so demanding it here would be demanding a
        # variable the code cannot read.
        #
        # It tracks configuration rather than freezing it: fill in derekt's
        # page_id and FB_PAGE_TOKEN_DEREKT becomes required by this test on the
        # same commit.
        for account in brand.publishable_accounts:
            if account.platform not in PUBLISHABLE_PLATFORMS:
                continue
            required.setdefault(
                token_env_var(brand.slug, account.platform),
                "app/credentials/tokens.py",
            )

    return required


def test_env_example_exists():
    """.gitignore un-ignores it explicitly (`!.env.example`) and README points
    at it. Its absence is what made the drift possible."""
    assert ENV_EXAMPLE.is_file(), (
        "No .env.example. It is the only checked-in list of what a deployment "
        "needs, and .gitignore already carves out an exception for it."
    )


def test_every_variable_the_code_requires_is_documented():
    """The check the README's hand-written list could not do for itself."""
    missing = {
        name: where
        for name, where in _required_by_the_code().items()
        if name not in _documented()
    }
    assert not missing, (
        "Read by the code but absent from .env.example:\n"
        + "\n".join(f"  {name}  ({where})" for name, where in sorted(missing.items()))
    )


def test_no_real_secret_is_committed_in_the_template():
    """A template full of working tokens is worse than no template.

    Every value must be empty or an obvious placeholder -- this file is
    committed, which is the entire difference between it and .env.
    """
    if not ENV_EXAMPLE.is_file():
        pytest.skip("covered by test_env_example_exists")

    offenders = []
    for number, line in enumerate(
        ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        value = match.group(2).strip().strip("\"'")
        if not value:
            continue
        # Placeholders only. A real Slack bot token starts xoxb- and a real
        # Page token is 200-odd characters of base64.
        if value.startswith(("<", "your-", "changeme", "TODO")) or value.isdigit():
            continue
        offenders.append(f"  line {number}: {match.group(1)}={value[:24]}")

    assert not offenders, (
        "These look like real values in a committed file:\n" + "\n".join(offenders)
    )
