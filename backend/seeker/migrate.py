"""Explicit, transactional schema migration: python -m seeker.migrate.

Existing 0011–0015 columns are baselined only after their idempotent upgrade
has succeeded. Checksums cover the exact bytes of each immutable migration.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, select, text

from .config import Settings
from .db import Base, SchemaVersion, connect

BUILD_FAMILY = "SKR"
REQUIRED_VERSION = "0019"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _numbered_migrations():
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.py")):
        source = path.read_bytes()
        namespace = {}
        exec(compile(source, str(path), "exec"), namespace)
        migrations.append((path.name[:4], hashlib.sha256(source).hexdigest(), namespace["COLUMNS"]))
    versions = [version for version, _, _ in migrations]
    expected = [f"{version:04d}" for version in range(11, int(REQUIRED_VERSION) + 1)]
    if versions != expected:
        raise RuntimeError("Invalid migration manifest / required version")
    return migrations


def _identity(conn):
    """Reject the other deployment even before adopting a pre-ledger database.

    The former cross-token renames are deliberately prohibited: deployment is not
    an authorization to convert balances between token families.
    """
    other = "usdc" if BUILD_FAMILY == "SKR" else "skr"
    inspector = inspect(conn)
    for table, columns in (
        ("seeker_actors", {f"{other}_funded", f"staked_{other}"}),
        ("judgments", {f"earned_{other}", f"settled_{other}"}),
    ):
        if inspector.has_table(table):
            if columns & {c["name"] for c in inspector.get_columns(table)}:
                raise RuntimeError(f"Schema build family mismatch: {BUILD_FAMILY} build cannot use {other.upper()} columns")


def _ledger(conn, migrations):
    if not inspect(conn).has_table("schema_versions"):
        return {}
    rows = {row.version: row for row in conn.execute(select(SchemaVersion.__table__))}
    checksums = {version: checksum for version, checksum, _ in migrations}
    for version, row in rows.items():
        if row.build_family != BUILD_FAMILY:
            raise RuntimeError(f"Schema build family mismatch: expected {BUILD_FAMILY}, found {row.build_family}")
        if version not in checksums:
            raise RuntimeError(f"Schema version {version} is not supported by this build")
        if row.checksum != checksums[version]:
            raise RuntimeError(f"Schema migration {version} checksum mismatch; restore the original migration")
    return rows


def _check_columns(conn):
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            raise RuntimeError(f"schema not applied — run migrations: missing table {table.name} (python -m seeker.migrate)")
        missing = set(table.columns.keys()) - {c["name"] for c in inspector.get_columns(table.name)}
        if missing:
            raise RuntimeError(f"schema not applied — run migrations: missing {table.name} columns {sorted(missing)}")


def check_schema(engine):
    """Read-only startup/readiness check. No create_all, ALTER or ledger writes."""
    migrations = _numbered_migrations()
    with engine.connect() as conn:
        rows = _ledger(conn, migrations)
        _identity(conn)
        missing = [v for v, _, _ in migrations if v not in rows]
        if missing:
            raise RuntimeError(
                f"schema not applied — run migrations: {BUILD_FAMILY} requires {REQUIRED_VERSION}; "
                f"missing {', '.join(missing)} (python -m seeker.migrate)"
            )
        _check_columns(conn)


def apply_migrations(engine):
    """Return the number of newly recorded versions; reruns are a safe no-op.

    All DDL and ledger rows commit together. PostgreSQL migrators serialize on a
    transaction advisory lock shared across both builds; SQLite uses BEGIN IMMEDIATE.
    """
    migrations = _numbered_migrations()
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(1936024939)"))
        rows = _ledger(conn, migrations)
        _identity(conn)
        pending = [m for m in migrations if m[0] not in rows]
        if not pending:
            _check_columns(conn)
            return 0
        # Bootstrap missing tables only in the explicit migrator. Existing
        # tables still require the numbered ALTERs below.
        Base.metadata.create_all(conn)
        for version, checksum, columns in pending:
            for table, column, ddl in columns:
                existing = {c["name"] for c in inspect(conn).get_columns(table)}
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.execute(SchemaVersion.__table__.insert().values(
                version=version, checksum=checksum,
                applied_at=datetime.now(timezone.utc), build_family=BUILD_FAMILY,
            ))
        _check_columns(conn)
        return len(pending)


def main():
    settings = Settings()
    engine = connect(settings.database_url)
    try:
        applied = apply_migrations(engine)
        print(f"{BUILD_FAMILY} schema {REQUIRED_VERSION}: {applied} migration version(s) recorded")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
