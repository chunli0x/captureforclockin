"""Baseline the schema ledger and deployment family; runtime performs no DDL.

schema_versions is created by the explicit migrator's metadata bootstrap.
seeker_rates already exists and is reused by the durable request limiter.
"""

COLUMNS = []
