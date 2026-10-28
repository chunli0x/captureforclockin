# Security audit register — HIGH + MED findings

Recovered from the parallel Astra security/confidentiality audit (September 2026, SKR + USDC builds).
Original verdict: **no CRITICAL**. SKR — 3 HIGH / 12 MED · USDC — 5 HIGH / 12 MED (findings are near-parallel; same codebase).

This register is the reconciliation source for phase 0 of BUILD-SCOPE.md. Status is preliminary and must be re-verified against current code before any real-value launch. "Fixed" means addressed by later work; the commit/evidence must be re-confirmed.

## HIGH (5 — already addressed)

| # | Finding | Location | Status |
|---|---|---|---|
| H1 | Liveness disabled (USDC `SEEKER_LIVENESS_REQUIRED=false`) | `backend/.env`, `api.py` | Intended (Colosseum); liveness now flag-gated (`SEEKER_LIVENESS_DEMO`) |
| H2 | Fabricated liveness — server accepts any nonzero digest (`"01"`) | `liveness.py`, `api.py` | Fixed — demo gated; real mode rejects demo digest |
| H3 | Faucet SOL drain — 0.05 SOL ×5, no global budget; retry over-caps | `api.py`, `chain.py` | Fixed — `faucet_treasury_sol_floor` + worker faucet job |
| H4 | Confidential material in reachable git history (570 "robot", 129 "pet", 29 "dog", 5 "animal") | git history | Left as-is per Lisa (2026-09) |
| H5 | Anti-spoof fails open — classifier load error → motion-only | `SpoofClassifier.kt`, `LivenessScreen.kt` | Partially — motion-only fallback retained; device attestation still `none` |

## MED (12 — the reconciliation target)

| ID | Finding | Location | Fix | Status |
|---|---|---|---|---|
| M1 | Standalone `warn` non-idempotent (no offense/resolution ID) | `lib.rs`, `chain.py` | Immutable single-use enforcement ID | Verify |
| M2 | Bond confirmation PostgreSQL lock-order deadlock (`Actor→Prepared` vs `Prepared→Actor`) | `api.py` | One lock order + retry deadlock victims | Likely fixed (outbox refactor) |
| M3 | Durable judgment obligations lack automatic recovery | `api.py`, `SeekerViewModel.kt` | Reconciliation worker + client recovery | Fixed (outbox + reconciler) |
| M4 | Batch-anchor retry can't recover an already-landed tx | `chain.py`, `api.py`, `lib.rs` | Read existing account, verify root | Fixed (reconciler) |
| M5 | Fresh deposits retain stale warning state | `api.py`, `lib.rs` | Reconcile on-chain bond state on confirm | Verify |
| M6 | Anti-spoof crop uses mismatched coordinates (rotation) | `LivenessScreen.kt` | Rotate bitmap before cropping | Fixed (`e8b8845`) |
| M7 | Android withdrawal/recovery UI unreachable; `s.error` not rendered | `BalanceScreen.kt`, `SeekerViewModel.kt` | Expose withdrawal + error/retry controls | Verify |
| M8 | Broken image traps judging UI (no skip/exclude) | `JudgmentScreen.kt`, `api.py` | Unpaid skip/unavailable-media op | Verify |
| M9 | CI distributes emulator-only debug build (`10.0.2.2:8001`) | `ci.yml`, `build.gradle.kts` | HTTPS backend via release path | Fixed (release build) |
| M10 | Program identity inconsistent across bootstrap/config | `Anchor.toml`, `lib.rs`, `dev_bootstrap.sh`, `.env.example` | Align all config per environment | Fixed (`22eca2e` + program-ID patch) |
| M11 | PostgreSQL CI references deleted tests (4 absent files) | `.github/workflows/seeker.yml` | Update to existing tests | Partial |
| M12 | Rolling abstention cap is actually lifetime (`_flat_payments_recent`) | `config.py`, `api.py` | Real window or relabel as lifetime | Open (scoped H9) |

## Verified safeguards (at audit time — re-confirm)

- Open on-chain liabilities block withdrawal.
- Payment receipts bind exact payment + mint atomically.
- Judgment IDs + signed payment bytes persist before broadcast.
- `resolve_judgment` retries don't repeat penalties.
- Gitleaks across 135 commits: no committed credentials.
- Release signing requires credentials; `minSdk=26`.

## Next step (phase 0)

Re-verify each MED against current code (both builds), flip "Verify" to Fixed/Open with commit evidence, and re-open any that regressed. The HIGHs are already dispositioned.
