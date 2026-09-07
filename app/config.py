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

from functools import lru_cache

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
                return f"{ASYNC_DRIVER}://{url[len(synonym):]}"

        if url.startswith(f"{ASYNC_DRIVER}://"):
            return url

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
