"""Reserve pre-upgrade accepted judgments before reopening the API.

Run `python -m seeker.liabilities` only AFTER upgrading the on-chain program.
This command writes on-chain; normal schema migration does not.
"""

from sqlalchemy import select

from .db import Judgment, JudgmentPair


def reserve_legacy(db, chain, pair_id):
    # Caller holds the pair row lock. A failed confirmation rolls back the DB
    # marker; the on-chain judgment ID makes the next reservation a no-op.
    rows = db.scalars(select(Judgment).where(
        Judgment.pair_id == pair_id, Judgment.liability_legacy.is_(True),
    ).order_by(Judgment.wallet)).all()
    for row in rows:
        if not row.consumed:
            chain.reserve_judgment(row.wallet, row.judgment_id)
            row.liability_reserved = True
        row.liability_legacy = False


def main():
    from .chain import Chain
    from .config import Settings
    from .db import Database

    settings = Settings()
    database, chain = Database(settings.database_url), Chain(settings)
    chain.check_environment()
    with database.transaction() as db:
        ids = list(db.scalars(select(JudgmentPair.pair_id).where(
            JudgmentPair.status != "resolved",
        )))
    for pair_id in ids:
        with database.transaction() as db:
            pair = db.scalar(select(JudgmentPair).where(
                JudgmentPair.pair_id == pair_id,
            ).with_for_update())
            if pair.status != "resolved":
                reserve_legacy(db, chain, pair_id)
    print("Legacy open judgment liabilities reserved")


if __name__ == "__main__":
    main()
