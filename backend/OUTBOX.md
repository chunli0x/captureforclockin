# Durable settlement

Judgment submission returns **202** with `judgment_id` and `operation_id`. The
immutable answer, wallet-scoped `Idempotency-Key` binding, and reservation job
commit together. Repeating a key with the same validated JSON body (including
pair ID) returns the same operation; a different body returns **409**. A new key
cannot change an existing vote. Keys are hashed in storage.

Clients poll `GET /judgments/{judgment_id}` or `GET /operations/{operation_id}`
with the original wallet session. Both return typed `accepted`, `pending`,
`finalized`, or `failed` states, an independent payout state/signature, pair
progress, and the component jobs. Substantive judgments finalize after batch
anchoring; neutral/cancelled judgments finalize after release. Payouts can be
finalized while enforcement or anchoring is pending. Calibration remains
synchronous and unpaid.

## PM rollout

Code changes do not start a worker or migrate a running database. During the
planned release, apply `python -m seeker.migrate` (schema **0017**, build family
must match), then run the API and a supervised **separate process**:

```sh
python -m seeker.worker
```

`python -m seeker.worker --once` performs one reconciliation pass and one small
claim batch for diagnostics. It does real chain work. No worker runs in HTTP
handlers or application startup. Multiple worker processes are supported.

## Execution and recovery

Jobs use `FOR UPDATE SKIP LOCKED` on Postgres; SQLite's `BEGIN IMMEDIATE` is the
local test fallback. Claims commit immediately. Each claim increments a fencing
version and carries a 120-second lease. Every worker journal write, including
signed payout bytes, checks the owner, version and unexpired lease. Completion
and retry release use compare-and-set. Leases can expire during slow RPCs; a
replacement reconciles chain receipts, while stale workers cannot write locally.

Reservation is atomic on chain with the collateral check. Consensus and payout
obligations commit once under the pair lock. All payouts must finish before
substantive enforcement decisions/statistics are journaled. Neutral votes never
count as dissent. Signed payout bytes commit before broadcast; exact payment,
liability and batch receipts remain the guard against repeated effects.

The worker reconciles every 30 seconds using bounded keyset pages: missing
reservations, pending payouts/enforcement, finalized unanchored batches, expired
leases, and unconfirmed prepared bond transactions. The legacy liabilities CLI
now queues recovery for the same worker. A returning user is not required.
Waiting for quorum or another batch pair delays work without using the failure
budget. Failures use exponential backoff with jitter (capped at roughly one
hour), with at most eight attempts. Abruptly expired leases count toward this
budget. Exception text, RPC URLs and signed bytes are never put in `last_error`
or worker error logs.

## Dead letters

`jobs.status = 'failed'` is durable and terminal for automatic retries. The worker
logs dead-letter IDs when an attempt exhausts its budget and logs the total on
reconciliation. Judgment polling exposes failures; unresolved prepared operations
can also be inspected by entity ID in the jobs table. Monitor these logs and the
failed-job count. Reconciliation never deletes or silently reopens failed jobs.
After investigating/fixing the cause, an operator can requeue the **same job**
(reset attempts, set pending and next_attempt_at, clear its lease, and increment
fencing_version in one transaction). Keep its ID, operation version and all
judgment/payment journals intact. Receipts are still checked on replay.

Bond prepare/confirm RPCs and faucet RPCs also run outside database transactions.
Faucet quota is reserved before RPC and conservatively retained on an ambiguous
failure, preventing concurrent callers from exceeding the cap.
