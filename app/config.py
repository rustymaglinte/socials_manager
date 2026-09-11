"""Configuration read from the environment, validated once.

Scoped to the database for now, deliberately. The Slack, Meta and provider
credentials are already read where they are used (`client.py` at import,
`fb_publish.py` per invocation), and sweeping them in here would be a refactor
of working code rather than part of standing the store up. The shape is built to
grow -- add a field, not a module.

Nothing is read at import. `client.py` builds its Slack app at import time and
`tests/conftest.py` carries a note explaining the fake tokens that exist only to
survive it; repeating that mistake would mean every test importing anything
under `app.store` needed a DATABASE_URL. `settings()` is called, cached, and
clearable -- the same shape as `load_brand`.
"""

import re
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The only driver this app runs on. Spelled once so the validator, the error
# messages and the engine cannot disagree about it.
ASYNC_DRIVER = "postgresql+asyncpg"

# Schemes a hosted provider might hand you, all meaning the same database.
# `postgres://` is what Neon, Supabase, Render and Heroku print; SQLAlchemy has
# not accepted it since 1.4, and the resulting error names neither the variable
# nor the fix.
_SYNONYMS = ("postgresql://", "postgres://")

# A URL's password, wherever one turns up in free text. `Settings.
# redacted_database_url` handles the DSN this module owns; this handles the
# same secret arriving inside somebody else's sentence -- which is where it
# actually shows up, because a driver that cannot connect puts the whole
# connection string into its exception, and that exception gets logged and,
# in `app.transports.slack_approval.handlers`, posted into a channel.
#
# Deliberately narrow: it matches `scheme://user:password@` and replaces only
# the password. The host and database name are what tell an operator which
# database refused them, and neither is a secret -- redaction that ate those
# would trade one unusable error report for another.
_CREDENTIALS = re.compile(r"(?P<prefix>[a-zA-Z][\w+.-]*://[^\s:/@]+):[^\s@]*@")


def redact(text: str) -> str:
    """Mask any URL password in `text`. Safe to call on anything.

    Used on the way out to somewhere a person can read: a log line, a Slack
    message. C-5 is about where a connection string ends up, not about which
    variable it came from, so it applies to a string that merely contains one.
    """
    return _CREDENTIALS.sub(r"\g<prefix>:***@", text)


def _asyncpg_tls(url: str) -> str:
    """Rewrite libpq's `sslmode` as the `ssl` asyncpg answers to.

    The same class of mistake as the scheme above, and it arrives on the same
    DSNs. `sslmode` is libpq's spelling; psycopg2 reads it, asyncpg does not.
    SQLAlchemy passes a query parameter it does not recognise straight through
    to `asyncpg.connect()`, so the failure is a TypeError naming a keyword the
    operator never typed -- against a connection string their database provider
    handed them, on the one code path that has no other way to work.

    Railway's *public* DATABASE_URL carries `?sslmode=require`, which is the URL
    you paste to run a migration from a laptop or to point a second service at
    the database. So this is not a courtesy: it is the difference between a
    deploy that connects and an error about an argument nobody wrote.

    Translated rather than dropped. `require` and `verify-full` are different
    promises, and quietly discarding the parameter would downgrade a verified
    connection to an unverified one -- a security change disguised as a
    compatibility fix. asyncpg understands every value libpq defines, under the
    other name, so the value crosses unchanged.

    A DSN that says neither `sslmode` nor nothing at all comes back byte for
    byte: re-encoding a query string we have no opinion about is a way to be
    surprised by a parameter we never meant to touch.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "sslmode" for key, _ in pairs):
        return url

    if any(key == "ssl" for key, _ in pairs):
        # Both spellings present means somebody renamed one and forgot the
        # other. The asyncpg-native name is the one that was going to be read;
        # keeping `sslmode` beside it would hand `connect()` the very keyword
        # this function exists to remove.
        kept = [(key, value) for key, value in pairs if key != "sslmode"]
    else:
        # Rewritten in place rather than appended, so the parameter a reader
        # goes looking for is where they left it.
        kept = [
            ("ssl", value) if key == "sslmode" else (key, value)
            for key, value in pairs
        ]

    return urlunsplit(parts._replace(query=urlencode(kept)))


class ConfigurationError(RuntimeError):
    """The environment cannot be honoured.

    Separate from pydantic's ValidationError because the audience is an operator
    who mistyped a variable, not a developer who passed a bad argument -- so the
    message names the variable and what to set it to.
    """


class Settings(BaseSettings):
    """Everything the store needs to open a connection."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore, not forbid: .env holds SLACK_*, META_* and FB_PAGE_TOKEN_*
        # that this model has no opinion about, and forbidding them would make
        # adding an unrelated variable break the database connection.
        extra="ignore",
    )

    database_url: str

    # Off by default; turning it on logs every statement, which is how you find
    # out what the publisher's claim query actually compiled to.
    db_echo: bool = False

    # Small on purpose. The processes here are one Slack listener and one or two
    # workers, each with a handful of concurrent transactions -- not a web tier.
    db_pool_size: int = 5
    db_max_overflow: int = 5

    @field_validator("database_url")
    @classmethod
    def _require_async_postgres(cls, url: str) -> str:
        """Normalise to the async driver, and refuse anything that is not Postgres.

        Not merely a convenience. `create_async_engine` given a sync driver
        fails somewhere far from the mistake, and the schema is not portable in
        any case: it uses JSONB, partial unique indexes, and the publisher will
        claim rows with `FOR UPDATE SKIP LOCKED`. A SQLite URL would open
        cleanly and then be quietly wrong about the two guarantees -- FR-13 and
        the approval gate -- that the store exists to provide.
        """
        url = url.strip()
        if not url:
            raise ValueError("DATABASE_URL is empty")

        for synonym in _SYNONYMS:
            if url.startswith(synonym):
                return _asyncpg_tls(f"{ASYNC_DRIVER}://{url[len(synonym):]}")

        if url.startswith(f"{ASYNC_DRIVER}://"):
            return _asyncpg_tls(url)

        scheme = url.split("://", 1)[0] if "://" in url else url
        raise ValueError(
            f"DATABASE_URL must point at Postgres, not {scheme!r}. This app uses "
            f"JSONB, partial unique indexes and SELECT ... FOR UPDATE SKIP "
            f"LOCKED; another backend would connect and then be wrong. Use "
            f"{ASYNC_DRIVER}://user:password@host:5432/dbname"
        )

    @property
    def redacted_database_url(self) -> str:
        """The DSN with the password removed, for logs and error messages.

        A connection string is the one setting that is both routinely logged and
        a secret (C-5). Anything that prints where it connected prints this.
        """
        url = self.database_url
        scheme, _, rest = url.partition("://")
        if "@" not in rest:
            return url

        credentials, _, host = rest.rpartition("@")
        user, sep, _password = credentials.partition(":")
        return f"{scheme}://{user}{':***' if sep else ''}@{host}"


@lru_cache(maxsize=1)
def settings() -> Settings:
    """The environment, read once.

    Cached so a mistyped variable is reported at the first call rather than
    intermittently. Call `settings.cache_clear()` in a test that changes the
    environment -- the entry is process-wide, exactly like `load_brand`'s.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # pydantic fills from env
    except ValidationError as error:
        # Pydantic's own rendering says "1 validation error for Settings /
        # database_url / Field required", which tells an operator nothing about
        # which variable to set or where it is documented.
        raise ConfigurationError(_explain(error)) from error


def _explain(error: ValidationError) -> str:
    missing = [
        detail["loc"][0]
        for detail in error.errors()
        if detail["type"] == "missing" and detail["loc"]
    ]
    if missing:
        names = ", ".join(str(name).upper() for name in missing)
        return (
            f"{names} is not set. Add it to .env (gitignored, never committed) "
            f"or your shell:\n\n"
            f"    DATABASE_URL={ASYNC_DRIVER}://user:password@localhost:5432/socials_manager\n\n"
            f"See the Environment section of README.md."
        )

    # A value that was present but unusable; the validator above already wrote a
    # sentence worth reading, so surface it rather than pydantic's wrapper.
    return "; ".join(detail["msg"].removeprefix("Value error, ") for detail in error.errors())
