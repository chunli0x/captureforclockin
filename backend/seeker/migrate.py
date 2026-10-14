"""Apply Seeker schema: create_all for fresh DBs plus numbered, idempotent
column migrations for upgrades. Run `python -m seeker.migrate`."""

import importlib.util
from pathlib import Path

from sqlalchemy import inspect

from .config import Settings
from .db import Base, connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Legacy USDC -> SKR column renames. Rename USDC-era columns (preserving
# balances) before the ADD COLUMN pass. Existing SKR-era databases already
# have the correct names and need no rename.
RENAMES = [
    ("seeker_actors", "usdc_funded", "skr_funded"),
    ("seeker_actors", "staked_usdc", "staked_skr"),
    ("judgments", "earned_usdc", "earned_skr"),
    ("judgments", "settled_usdc", "settled_skr"),
]


def _numbered_migrations():
    """Yield (migration_id, columns) for every NNNN_*.py module in migrations/,
    in filename order. Each module exposes a COLUMNS list of
    (table, column, ddl) triples."""
    for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(
            "seeker_migrations_" + path.stem, path
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield path.stem, module.COLUMNS


def apply_migrations(engine):
    """Idempotently apply every numbered migration. A column is added with
    ``ALTER TABLE … ADD COLUMN`` only when it is absent, so an existing phase-1
    DB upgrades cleanly and re-running is a no-op. Works on both SQLite and
    PostgreSQL."""
    applied = 0
    for table, old, new in RENAMES:
        inspector = inspect(engine)
        if not inspector.has_table(table):
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if old in cols and new not in cols:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"
                )
            applied += 1
    for migration_id, columns in _numbered_migrations():
        for table, column, ddl in columns:
            inspector = inspect(engine)
            if not inspector.has_table(table):
                # Fresh DBs get the column via create_all; a table absent here
                # means there is nothing to upgrade for this column.
                continue
            if column in {c["name"] for c in inspector.get_columns(table)}:
                continue
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
            applied += 1
    return applied


def main():
    settings = Settings()
    engine = connect(settings.database_url)
    # create_all is additive and idempotent; numbered migrations handle the rest.
    Base.metadata.create_all(engine)
    apply_migrations(engine)
    print("Seeker schema applied (create_all + numbered migrations)")


if __name__ == "__main__":
    main()
