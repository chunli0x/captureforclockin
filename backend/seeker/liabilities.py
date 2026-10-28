"""Compatibility entry point: queue legacy recovery without holding locks over RPC.

Run the regular worker to process reservations and settlement autonomously.
"""
from sqlalchemy import select
from .db import Database, Judgment
from .outbox import enqueue


def main():
    from .config import Settings
    from .migrate import check_schema
    database = Database(Settings().database_url)
    try:
        check_schema(database.engine)
        with database.transaction() as db:
            for jid in db.scalars(select(Judgment.judgment_id).where(
                Judgment.liability_legacy.is_(True), Judgment.enforcement_complete.is_(False))):
                enqueue(db, "reservation", jid)
        print("Legacy recovery queued; run python -m seeker.worker")
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
