"""One-off: repoint the seeded pairs at real, resolvable demo images.

The original seed wrote placeholder URLs (404). This rewrites output_a_url /
output_b_url on every judgment pair and calibration pair to the served demo
images (natural vs degraded), randomizing A/B order so the answer isn't fixed.
Run from the repo root:

    backend/.venv/bin/python3.11 scripts/update_seed_urls.py
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from seeker.config import Settings  # noqa: E402
from seeker.db import (  # noqa: E402
    CalibrationPair,
    JudgmentPair,
    connect,
    sessionmaker,
)
from sqlalchemy import func, select  # noqa: E402

BASE = "https://capture.fun/seed"
natural = [f"{BASE}/natural-{i}.png" for i in range(6)]
degraded = [f"{BASE}/degraded-{i}.png" for i in range(6)]


def main() -> None:
    settings = Settings()
    engine = connect(settings.database_url)
    Session = sessionmaker(engine)

    with Session.begin() as db:
        # Only rewrite pairs that have not started collecting judgments; a pair
        # with votes (or a resolved anchor) must not have its images changed
        # under the judges (finding #8).
        pairs = list(db.execute(
            select(JudgmentPair)
            .where(JudgmentPair.status == "open")
            .order_by(JudgmentPair.pair_id)
        ).scalars())
        for idx, p in enumerate(pairs):
            i = idx % 6
            if p.task_type == "preference":
                swapped = (idx // 6) % 2 == 1
                p.output_a_url = degraded[i] if swapped else natural[i]
                p.output_b_url = natural[i] if swapped else degraded[i]
            else:
                p.output_a_url = degraded[i] if idx % 2 else natural[i]
                p.output_b_url = None

        cals = list(db.execute(select(CalibrationPair).order_by(CalibrationPair.pair_id)).scalars())
        for idx, c in enumerate(cals):
            i = idx % 6
            if c.known_choice == "b":
                # The known-correct answer is B, so B carries the natural image.
                c.output_a_url = degraded[i]
                c.output_b_url = natural[i]
            else:
                c.output_a_url = natural[i]
                c.output_b_url = degraded[i]

    with Session() as db:
        np_ = db.scalar(select(func.count()).select_from(JudgmentPair))
        nc_ = db.scalar(select(func.count()).select_from(CalibrationPair))
        pref = db.scalar(
            select(func.count()).select_from(JudgmentPair).where(JudgmentPair.task_type == "preference")
        )
        rating = np_ - pref

    print(f"Updated {np_} judgment pairs ({pref} preference + {rating} rating) + {nc_} calibration pairs")


if __name__ == "__main__":
    main()
