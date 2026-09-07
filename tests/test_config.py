"""app.config -- the environment, validated once.

The normalisation tests are not pedantry: every hosted Postgres hands out a DSN
this app cannot use verbatim, and the error you get from passing one straight to
`create_async_engine` names neither the variable nor the fix.
"""

import pytest
from pydantic import ValidationError

from app.config import ASYNC_DRIVER, ConfigurationError, Settings, settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`settings()` is cached process-wide, exactly like `load_brand`.

    Cleared on both sides: an entry built from one test's environment would
    otherwise satisfy the next test, making order matter.
    """
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.mark.parametrize(
    "raw",
    [
        # What Neon, Supabase, Render and Heroku print. SQLAlchemy has not
        # accepted this scheme since 1.4.
        "postgres://user:pw@db.example.com:5432/socials",
        # What psql and every tutorial use.
        "postgresql://user:pw@db.example.com:5432/socials",
        # Already correct; must survive untouched.
        "postgresql+asyncpg://user:pw@db.example.com:5432/socials",
    ],
)
def test_every_way_of_spelling_postgres_reaches_the_async_driver(raw):
    assert Settings(database_url=raw).database_url == (
        f"{ASYNC_DRIVER}://user:pw@db.example.com:5432/socials"
    )


def test_surrounding_whitespace_is_forgiven():
    """A DSN pasted into .env picks up a trailing space more often than not."""
    url = Settings(database_url="  postgresql://u:p@h:5432/d  ").database_url
    assert url == f"{ASYNC_DRIVER}://u:p@h:5432/d"


@pytest.mark.parametrize(
    "raw",
    [
        # Would open cleanly and then be wrong about FR-13 and the approval
        # gate: no JSONB, no partial unique indexes, no SKIP LOCKED.
        "sqlite+aiosqlite:///./socials.db",
        "mysql+aiomysql://u:p@h/d",
        # A sync driver fails inside create_async_engine, far from the mistake.
        "postgresql+psycopg2://u:p@h:5432/d",
        "",
    ],
)
def test_anything_that_is_not_async_postgres_is_refused(raw):
    with pytest.raises(ValidationError):
        Settings(database_url=raw)


def test_the_refusal_explains_why_rather_than_naming_a_scheme():
    with pytest.raises(ValidationError, match="SKIP LOCKED"):
        Settings(database_url="sqlite+aiosqlite:///./socials.db")


def test_a_missing_variable_says_which_one_and_what_to_set_it_to(monkeypatch):
    """Pydantic's own rendering is "Field required", which helps nobody."""
    # env_file as well as the environment: a .env with a DATABASE_URL in it
    # would otherwise make this test pass or fail depending on the machine.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError) as caught:
        settings()

    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert ASYNC_DRIVER in message  # shows the shape, not just the name
    assert "README" in message


def test_the_environment_is_read_once(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/first")
    first = settings()

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/second")
    assert settings() is first, "settings() must be cached, like load_brand"

    settings.cache_clear()
    assert settings().database_url.endswith("/second")


# --- redaction -------------------------------------------------------------
#
# A DSN is the one setting that is both routinely logged and a secret (C-5).


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgresql://rusty:hunter2@localhost:5432/socials",
            f"{ASYNC_DRIVER}://rusty:***@localhost:5432/socials",
        ),
        # No password to hide.
        (
            "postgresql://rusty@localhost:5432/socials",
            f"{ASYNC_DRIVER}://rusty@localhost:5432/socials",
        ),
        # No credentials at all -- a local socket or a trusted host.
        (
            "postgresql://localhost:5432/socials",
            f"{ASYNC_DRIVER}://localhost:5432/socials",
        ),
    ],
)
def test_the_password_never_reaches_a_log_line(raw, expected):
    assert Settings(database_url=raw).redacted_database_url == expected


def test_a_password_containing_an_at_sign_is_still_hidden():
    """`@` is legal in a password and splitting on the first one would print it."""
    redacted = Settings(
        database_url="postgresql://rusty:p@ss@localhost:5432/socials"
    ).redacted_database_url
    assert "p@ss" not in redacted
    assert redacted == f"{ASYNC_DRIVER}://rusty:***@localhost:5432/socials"
