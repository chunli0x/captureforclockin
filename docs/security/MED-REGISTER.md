# Security audit register — HIGH + MED findings

Recovered from the parallel Astra security/confidentiality audit (September 2026, SKR + USDC builds).
Original verdict: **no CRITICAL**. SKR — 3 HIGH / 12 MED · USDC — 5 HIGH / 12 MED (near-parallel; same codebase).

Reconciled against current `main` on 2026-10-27. Status is verified against the current code with evidence; "Open" items are the remaining remediation backlog.

## HIGH (5 — dispositioned)

| # | Finding | Status |
|---|---|---|
| H1 | Liveness disabled (USDC) | Intended (Colosseum); now flag-gated |
| H2 | Fabricated liveness (any nonzero digest) | Fixed — demo gated, real mode rejects |
| H3 | Faucet SOL drain | Fixed — `faucet_treasury_sol_floor` + worker faucet job |
| H4 | Confidential git history | Left as-is per Lisa (2026-09) |
| H5 | Anti-spoof fails open | Partially — fallback retained; attestation still `none` |

## MED (12 — reconciled)

| ID | Finding | Status | Evidence |
|---|---|---|---|
| M1 | Standalone `warn` non-idempotent | **Fixed** | Settlement uses `resolve_judgment(resolution_id)` (`settlement.py:258`); legacy `warn` is uncalled dead code |
| M2 | Bond-confirmation lock-order deadlock | **Fixed** | `record_confirmation` releases locks between phases + locks Actor→Prepared consistently (`api.py:688`) |
| M3 | Judgment obligations lack automatic recovery | **Fixed** | Postgres outbox + fenced worker + reconciler (`c8e5857`) |
| M4 | Batch-anchor retry can't recover landed tx | **Fixed** | Reconciler reads existing account/root before re-init (`c8e5857`) |
| M5 | Fresh deposits retain stale warning state | **Fixed** | Deposit confirm mirrors authoritative on-chain warnings (`942e851`) |
| M6 | Anti-spoof crop coordinate mismatch | **Fixed** | Rotation fix (`e8b8845`) |
| M7 | Withdrawal/recovery UI unreachable | **Fixed** | Withdrawal/recovery control + `OperationFeedback` error/retry (`942e851`) |
| M8 | Broken image traps judging UI | **Fixed** | Unpaid media skip/exclude path (`942e851`) |
| M9 | CI distributes emulator-only debug build | **Fixed** | Release build + HTTPS URL (`d364a95`) |
| M10 | Program ID inconsistent across bootstrap | **Fixed** | `22eca2e` + program-ID patch |
| M11 | PostgreSQL CI references deleted tests | **Fixed** | `seeker.yml` repointed at real tests (`942e851`) |
| M12 | Rolling abstention cap actually lifetime | **Fixed** | `judge_flat_cap` + `_flat_payments_recent` window in `settlement.py:181` |

## Verified safeguards (at audit time — re-confirm)

- Open on-chain liabilities block withdrawal.
- Payment receipts bind exact payment + mint atomically.
- Judgment IDs + signed payment bytes persist before broadcast.
- `resolve_judgment` retries don't repeat penalties.
- Gitleaks across 135 commits: no committed credentials.
- Release signing requires credentials; `minSdk=26`.

## Result

**12 of 12 MED fixed.** All four remaining backlog items (M5, M7, M8, M11) were resolved in `942e851` (SKR) / `bf8b8b2` (USDC): authoritative warning reconciliation on deposit confirm, withdrawal/recovery controls with visible error+retry feedback, an unpaid media-skip path, and repaired CI test coverage. The HIGHs remain dispositioned as noted above.
