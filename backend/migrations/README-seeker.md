# Explicit Seeker schema migrations

Before starting `uvicorn seeker.api:production_app --factory`, use the deployment's
`SEEKER_*` configuration and a database role with DDL rights, from `backend/`:

```sh
.venv/bin/python -m seeker.migrate
```

The migrator bootstraps missing tables, applies the immutable 0011–0015 column
migrations, and records versions 0011–0018 in `schema_versions`. Each row contains
`version`, the SHA-256 of the exact migration file, UTC `applied_at`, and
`build_family` (`SKR` in this repository). Existing columns and balances are
preserved. Existing accepted votes missing `liability_legacy` receive its legacy
migration default, so they still require liability reservation.

DDL and ledger writes are one transaction. Concurrent PostgreSQL migrators use
the same transaction advisory lock; local SQLite uses `BEGIN IMMEDIATE`. A second
run reports zero recorded versions and performs no DDL or ledger writes. A
checksum mismatch fails before mutation; restore the original migration file,
never edit an applied migration. Add a new numbered migration for future schema
changes and advance `REQUIRED_VERSION` in `seeker/migrate.py`.

Startup and `/health` read the ledger, verify all required versions and checksums,
check the build family, and check required tables/columns. An empty or behind
database refuses startup with `schema not applied — run migrations`. Runtime
credentials need SELECT on `schema_versions` and DML on application tables, with
no schema modification privileges. Do not grant runtime ledger-write privileges.

Both startup and migration reject the other build's ledger or token columns.
The old automatic SKR/USDC column conversion is removed. Confirm the database
URL and use the matching build; converting an old cross-token deployment needs a
separate reviewed data migration. Do not change ledger identity to bypass this.
No systemd service is restarted by the migrator.

For local SQLite, set `SEEKER_LOCAL_SQLITE=true` and point `SEEKER_DATABASE_URL`
at a disposable file. The migrator uses `Settings`, so the normal program ID,
token mint and verifier key path settings must also be supplied; it does not
read the key or contact the chain.

## Request budgets

`seeker/rate_limit.py` applies admission limits to every HTTP route, including
invalid auth, malformed input and unknown routes. Atomic upserts into the existing
`seeker_rates` table commit separately from handlers, so errors cannot refund
budget. PostgreSQL supplies the clock and shared counters across workers. SQLite
is supported for local tests. A failed budget store returns 503; denied requests
return 429 with `Retry-After`. There is no Redis or in-memory fallback.

Default fixed windows:

| Bucket | Limit |
| --- | --- |
| Global, all requests | 3,000/minute |
| IP, all requests | 300/minute |
| IP, auth routes | 30/minute |
| IP, each expensive route category | 120/minute |
| Wallet, all identified requests | 120/minute |
| Wallet, auth | 20/minute |
| Wallet, judgment (all IDs) | 60/minute |
| Wallet, bond and transaction routes (all IDs/aliases) | 30/minute |
| Wallet, liveness issue / completion (separate) | 10/hour each |

Global/IP admission happens before reading the body or authenticating. Wallet
identity comes from a known session (including expired sessions), the requested
wallet at challenge issuance, or the stored challenge at verification. Unknown
credentials still consume global and IP budgets. Limits apply to failures as
well as success. Fixed windows can admit up to two windows' budget across a
boundary; this pass does not implement sliding windows.

Global/IP/general-wallet limits are configured by the `SEEKER_RATE_*` fields in
`seeker/config.py`. Requests are capped at 16 KiB for the body and 16 KiB for the
headers plus request target, with a 10-second body-read deadline (413, 431 and 408
respectively). The actual streamed body is bounded even without Content-Length.
These defaults are adjustable via `SEEKER_MAX_REQUEST_BODY_BYTES`,
`SEEKER_MAX_REQUEST_HEAD_BYTES`, and `SEEKER_REQUEST_BODY_TIMEOUT_SECONDS`.

The middleware uses the ASGI peer address and does not parse forwarded headers.
If a reverse proxy is used, configure uvicorn's trusted proxy addresses narrowly;
otherwise all proxied traffic shares one IP bucket. Header parsing and connection
limits should also be set at the deployment's HTTP server/proxy layer.

Counters reuse one row per hashed bucket identity across windows. They saturate
after denial; no new IP keys are allocated once the global budget is exhausted.
Old IP/wallet rows are retained; periodic retention maintenance is not part of
this pass.

## Verification

```sh
SEEKER_LOCAL_SQLITE=true .venv/bin/python -m pytest -q
```

`tests/test_durability.py` exercises explicit CLI migration twice, empty and
wrong-build startup, checksums, legacy adoption, transactional rollback, parallel
migrators, concurrent counters, restart durability, failure charging, and request
bounds. SBF tests require the separately compiled on-chain program.

## Liveness verification (0018)

The PM must apply migration 0018 before starting this version. It adds nullable
verification timestamps, method, expiry and device-integrity signal to actors.
The old `liveness_completed` column remains for additive schema compatibility,
but no gate reads it and old True values are not backfilled as verification.
Existing users need a new check. No deployed database is migrated by tests.

See [liveness policy and provider contract](../../docs/LIVENESS.md).
