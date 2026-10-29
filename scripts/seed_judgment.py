"""Seed the judgment product with demo data (calibration pairs + judgment pairs).

Operator/dev-only. Loads Settings from ``backend/.env`` (or the environment) and
inserts, idempotently:

- one judgment batch,
- ``calibration_n`` calibration pairs with known choices (enough for a judge to
  pass the calibration gate),
- a set of judgment pairs (A/B preference + 1-5 rating) across the batch.

Output URLs point at served demo images (natural vs degraded) at a fixed
768x768 resolution, so a judge sees real, resolvable content. Run from the
backend dir:

    .venv/bin/python ../scripts/seed_judgment.py
"""

import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Load backend/.env if present (SEEKER_* settings), then construct Settings.
try:  # pragma: no cover - dotenv is optional in some environments
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env")
except Exception:
    pass

from seeker.config import Settings  # noqa: E402
from seeker.db import (  # noqa: E402
    Base,
    CalibrationPair,
    JudgmentBatch,
    JudgmentPair,
    connect,
    sessionmaker,
)
from seeker.migrate import apply_migrations  # noqa: E402


def placeholder(batch_id, side, i):
    # Real served demo images (shared across batches). "a" = natural, "b" =
    # degraded, cycling the 6 images. Replace with the real vision-model outputs
    # when Lisa's pipeline commissions a batch.
    kind = "natural" if side == "a" else "degraded"
    return f"https://capture.fun/seed/{kind}-{i % 6}.png"


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def main():
    settings = Settings()
    engine = connect(settings.database_url)
    Base.metadata.create_all(engine)
    apply_migrations(engine)

    Session = sessionmaker(engine, expire_on_commit=False)

    with Session.begin() as db:
        # Create the batch only on first run (idempotent re-seed must not mint
        # a new empty batch each time).
        if count(db, JudgmentBatch) == 0:
            batch_id = uuid.uuid4().hex
            db.add(JudgmentBatch(batch_id=batch_id))
        else:
            batch_id = db.scalar(select(JudgmentBatch.batch_id))

        # Calibration pairs: enough to cover calibration_n (default 10), with a
        # known-consensus answer so a judge who judges well can pass.
        if count(db, CalibrationPair) == 0:
            for i in range(settings.calibration_n):
                db.add(
                    CalibrationPair(
                        pair_id=uuid.uuid4().hex,
                        known_choice="a" if i % 2 == 0 else "b",
                        known_consensus=0.9,
                        media_type="image",
                        task_type="preference",
                        output_a_url=placeholder(batch_id, "a" if i % 2 == 0 else "b", i),
                        output_b_url=placeholder(batch_id, "b" if i % 2 == 0 else "a", i),
                        rubric="prefer naturalness over diversity",
                    )
                )

        # Judgment pairs: fill up to JUDGMENT_PAIRS_TARGET with a 60/40
        # preference/rating mix so a demo has enough pairs for many judges.
        target = 100
        for i in range(count(db, JudgmentPair), target):
            is_pref = (i % 5) < 3  # 3 of 5 preference, 2 of 5 rating
            db.add(
                JudgmentPair(
                    pair_id=uuid.uuid4().hex,
                    batch_id=batch_id,
                    media_type="image",
                    task_type="preference" if is_pref else "rating",
                    output_a_url=placeholder(batch_id, "a", 100 + i),
                    output_b_url=None if not is_pref else placeholder(batch_id, "b", 100 + i),
                    rubric="which output is more natural" if is_pref else "rate quality 1-5",
                    k=settings.judge_k,
                )
            )

    with Session() as db:
        n_pairs = count(db, JudgmentPair)
        n_cal = count(db, CalibrationPair)
    print(f"Seeded batch {batch_id}: {n_cal} calibration pairs, {n_pairs} judgment pairs")
    print(f"(calibration_n={settings.calibration_n}, judge_k={settings.judge_k})")


if __name__ == "__main__":
    main()
