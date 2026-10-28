"""Durable outbox and wallet-scoped idempotency keys.

The explicit migrator creates missing tables from Base.metadata, including
jobs (JSONB on Postgres / text on SQLite), both uniqueness constraints and the
ready-job index, and judgment_requests. Existing tables need no ALTERs.
"""

COLUMNS = []
