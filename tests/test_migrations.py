"""The migrations build the schema the models describe.

The initial migration was hand-written -- Alembic's autogenerate compares
against a live database, and there was none. This is what makes that safe: the
migration's DDL is rendered offline and compared, statement by statement, with
the DDL of `Base.metadata`. A column added to a model and not to a migration
fails here rather than on the first `alembic upgrade` against production.

No database. Both sides are compiled against the Postgres dialect in memory.
"""

import ast
import importlib.util
import io
import re
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.store.models import Base

DIALECT = postgresql.dialect()
VERSIONS = Path(__file__).resolve().parents[1] / "app/store/migrations/versions"


def _split_clauses(body: str) -> frozenset[str]:
    """The contents of a CREATE TABLE, as an unordered set of clauses.

    Split at paren depth zero, so `CHECK (state IN ('a', 'b'))` survives whole.
    Unordered because SQLAlchemy emits constraints in declaration order, and a
    migration listing the same constraints in a different order builds the same
    table -- comparing raw text would fail on a difference that is not one.
    """
    clauses: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            clauses.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        clauses.append("".join(current).strip())
    return frozenset(clauses)


def _statements(sql: str) -> set:
    """DDL text -> a comparable set. CREATE TABLEs become (name, {clauses})."""
    sql = re.sub(r"--[^\n]*", " ", sql)  # alembic annotates its output
    out = set()
    for statement in sql.split(";"):
        collapsed = " ".join(statement.split()).strip()
        if not collapsed or collapsed.upper() in {"BEGIN", "COMMIT"}:
            continue
        table = re.match(r"CREATE TABLE (\w+) \((.*)\)$", collapsed)
        out.add(
            (table.group(1), _split_clauses(table.group(2))) if table else collapsed
        )
    return out


def _migration_ddl() -> set:
    """What every migration in versions/ would execute, in order.

    Run through Alembic's own offline machinery rather than by reading the
    files, so `op.create_table` renders exactly as it would against a database.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect=DIALECT, opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        for path in sorted(VERSIONS.glob("[0-9]*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()
    return _statements(buffer.getvalue())


def _metadata_ddl() -> set:
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=DIALECT)))
        for index in table.indexes:
            parts.append(str(CreateIndex(index).compile(dialect=DIALECT)))
    return _statements(";".join(parts))


def test_the_migrations_build_exactly_what_the_models_describe():
    migration, metadata = _migration_ddl(), _metadata_ddl()

    missing = metadata - migration
    extra = migration - metadata
    assert not missing, f"in the models but not in any migration: {missing}"
    assert not extra, f"in a migration but not in the models: {extra}"


def test_every_migration_can_be_undone():
    """A migration with no downgrade is one you cannot back out of at 2am."""
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        source = path.read_text(encoding="utf-8")
        body = source.split("def downgrade()", 1)
        assert len(body) == 2, f"{path.name} has no downgrade()"
        assert "pass" not in body[1].split("\n")[1], f"{path.name} downgrade is empty"


def test_migrations_do_not_import_the_models():
    """A migration is a snapshot of the schema on the day it was written.

    One that imported the models would follow them forward and quietly stop
    describing the database it actually built.

    Parsed rather than grepped: these files discuss the models in their
    docstrings, and a substring search cannot tell prose from an import.
    """
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        offenders = {
            name
            for name in imported
            if name.startswith(("app.store.models", "app.domain"))
        }
        assert not offenders, f"{path.name} imports {offenders}"


def test_revisions_form_one_chain():
    """Two heads means `alembic upgrade head` picks one and silently skips the
    other -- the failure mode that leaves half a schema in place."""
    revisions, parents = set(), []
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(f"chain_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions.add(module.revision)
        parents.append(module.down_revision)

    assert len(revisions) == len(parents), "duplicate revision id"
    assert parents.count(None) == 1, "expected exactly one root revision"
    # Every parent that is not the root names a revision that exists.
    assert {parent for parent in parents if parent} <= revisions


@pytest.mark.parametrize(
    "table",
    ["brands", "drafts", "post_variants", "post_media", "approvals",
     "scheduled_posts", "post_metrics", "post_engagements"],
)
def test_the_expected_tables_are_created(table):
    """Names the tables outright, so a rename is a deliberate edit here rather
    than something the set-comparison above absorbs on both sides at once."""
    created = {
        entry[0] for entry in _migration_ddl() if isinstance(entry, tuple)
    }
    assert table in created
