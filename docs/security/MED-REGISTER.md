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
| M5 | Fresh deposits retain stale warning state | **Open** | `record_confirmation` sets `bonded=True` but does not reconcile `warnings`; settlement reconciles on resolve only |
| M6 | Anti-spoof crop coordinate mismatch | **Fixed** | Rotation fix (`e8b8845`) |
| M7 | Withdrawal/recovery UI unreachable | **Open** | `SeekerViewModel.recover()` exists but no UI caller in `BalanceScreen`; `s.error` still not rendered |
| M8 | Broken image traps judging UI | **Open** | "try another" re-fetches the same failed pair; no skip/exclude/unavailable-media op |
| M9 | CI distributes emulator-only debug build | **Fixed** | Release build + HTTPS URL (`d364a95`) |
| M10 | Program ID inconsistent across bootstrap | **Fixed** | `22eca2e` + program-ID patch |
| M11 | PostgreSQL CI references deleted tests | **Open** | `seeker.yml:29` still lists `test_seeker_flow.py`, `test_seeker_audit_concurrency.py`, `test_seeker_slash.py`, `test_bond_gate.py` |
| M12 | Rolling abstention cap actually lifetime | **Fixed** | `judge_flat_cap` + `_flat_payments_recent` window in `settlement.py:181` |

## Verified safeguards (at audit time — re-confirm)

- Open on-chain liabilities block withdrawal.
- Payment receipts bind exact payment + mint atomically.
- Judgment IDs + signed payment bytes persist before broadcast.
- `resolve_judgment` retries don't repeat penalties.
- Gitleaks across 135 commits: no committed credentials.
- Release signing requires credentials; `minSdk=26`.

## Result

**8 of 12 MED fixed; 4 remain open** (M5, M7, M8, M11 — all minor correctness/UX/CI items).

Remaining backlog in priority order:
1. **M11** — fix `seeker.yml` test paths (trivial, CI hygiene).
2. **M8** — add a skip/exclude for broken media (image UX).
3. **M7** — wire withdrawal/recovery UI + render `s.error`.
4. **M5** — reconcile bond/warning state on deposit confirm.
