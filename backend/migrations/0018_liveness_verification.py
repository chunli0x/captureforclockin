"""Expiring liveness evidence. Legacy Boolean flags are not backfilled."""

COLUMNS = [
    ("seeker_actors", "liveness_verified_at", "BIGINT"),
    ("seeker_actors", "liveness_method", "VARCHAR(16)"),
    ("seeker_actors", "liveness_expires_at", "BIGINT"),
    ("seeker_actors", "liveness_device_integrity", "VARCHAR(64)"),
]
