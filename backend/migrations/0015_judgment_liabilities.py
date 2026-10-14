"""Durable reservation, enforcement and signed payout journals."""
COLUMNS = [
    # Existing accepted votes need reservations before accepting more work.
    ("judgments", "liability_legacy", "BOOLEAN NOT NULL DEFAULT TRUE"),
] + [
    ("judgments", name, "BOOLEAN NOT NULL DEFAULT FALSE")
    for name in ("liability_reserved", "enforcement_requested", "enforcement_penalty", "enforcement_complete")
] + [
    ("judgments", "payout_transaction", "VARCHAR(4096)"),
    ("judgments", "payout_pending_signature", "VARCHAR(88)"),
    ("judgments", "payout_last_valid_height", "BIGINT"),
]
