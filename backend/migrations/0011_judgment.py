"""Migration 0011 — judgment product (RLHF preference judging) columns.

The judgment product (Capture pivot) adds per-judge columns to ``seeker_actors``:
calibration state, rolling agreement rate, resolved-judgment count, staked SKR
mirror, and the on-chain warning-ladder mirror. The new judgment/calibration
tables themselves are created by ``create_all`` (additive); only these Actor
columns need an ALTER TABLE on an existing deployment.
"""

COLUMNS = [
    ("seeker_actors", "calibrated", "BOOLEAN DEFAULT FALSE"),
    ("seeker_actors", "agreement_rate", "DOUBLE PRECISION DEFAULT 0.0"),
    ("seeker_actors", "judged_count", "INTEGER DEFAULT 0"),
    ("seeker_actors", "staked_skr", "BIGINT DEFAULT 0"),
    ("seeker_actors", "warnings", "INTEGER DEFAULT 0"),
]
