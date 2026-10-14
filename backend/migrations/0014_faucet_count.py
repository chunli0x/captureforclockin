"""Migration 0014 — faucet mint counter.

Adds ``seeker_actors.faucet_count`` so the devnet test-SKR faucet can be
capped at ``faucet_max_mints`` taps per wallet instead of a single idempotent
mint. ``skr_funded`` stays as the "funded at least once" flag for the deposit
gate; ``faucet_count`` is the running tally.
"""

COLUMNS = [
    ("seeker_actors", "faucet_count", "INTEGER DEFAULT 0"),
]
