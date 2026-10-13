# Capture

**I judge vision-model outputs. Consensus decides who gets paid.**

We designed Capture for the Solana Seeker mobile. Capture is a mobile RLHF
preference-judging network. A judge sees
two vision-model outputs and picks the better one (A/B preference) or rates a
single output 1–5. Aggregated preferences become an RLHF preference dataset,
with trust priced into every judgment through a slashable **test $SKR** stake.

## The judgment product

Humans judge a vision model's outputs for RLHF. The incentive is
**consensus-gated pay in test $SKR**: every pair goes to K=3 judges; the
majority pick (or the crowd median for ratings) wins, aligned judges are paid
(`base_skr × level multiplier`), dissenters earn nothing, and **"can't tell" is
a first-class answer** that re-routes the pair instead of forcing a guess.

The full loop:

1. **Stake.** I sign in with Seeker Wallet and deposit a test $SKR bond on-chain. The
   bond is the stake I can lose for systematic disagreement.
2. **Judge.** The judge answers A/B or 1–5 pairs. One judge cannot answer the
   same pair twice.
3. **Resolve.** K substantive votes resolve the pair. Neutral votes ("can't
   tell"/"tie") never count as dissent and never inflate the quorum. A genuine
   split adds one tiebreaker judge.
4. **Pay or slash.** Aligned judges earn test $SKR (journaled before broadcast,
   idempotent per judgment). A judge whose rolling agreement rate stays below a
   floor runs the two-strike warn/slash ladder on-chain (full bond forfeit on
   the second strike).
5. **Anchor.** Each fully-resolved batch records a Merkle root and commits it
   on-chain via `post_batch` as dataset provenance (append-only per batch; a
   failed final anchor is retried idempotently).

A judge-accuracy ladder (agreement-rate driven) raises the rate multiplier from
1.00 up to 2.00 across five levels.

Honest notes (state, do not hide):

- **Test tokens.** Pay and bond are both **devnet/test $SKR** (one token, three
  jobs). Not real value. The UI and `/health` both say so.
- **Majority-preference bias.** RLHF on majority preference flattens minority and
  edge preferences. This is fine for a first pass, but the dataset does **not**
  represent all humans. Do not claim it does.
- K, thresholds, and rates are starting hypotheses, tune against real agreement
  data, not once.
- **Seed data.** The demo is seeded with 100 judgment pairs whose output URLs
  are placeholders (real vision-model outputs are loaded by the training
  pipeline, out of scope for the hackathon). This proves the judging
  infrastructure, not a production dataset.

## Payout economics

I earn a base **0.02 $SKR per aligned judgment**, times my level
multiplier. Dissent earns **0 $SKR**. "Can't tell" earns a flat **0.005 $SKR**
and re-routes the pair. These are **test $SKR on devnet, not real value**.
The token uses 6 decimals; these amounts are not USD prices.

| Level | Agreement | Rate multiplier |
|---|---|---|
| Scout | no gate | ×1.00 |
| Regular | ≥80% | ×1.10 |
| Veteran | ≥88% | ×1.25 |
| Expert | ≥93% | ×1.50 |
| Pathfinder | ≥97% | ×2.00 |

My agreement rate determines my level multiplier. Payout and bond use the
same test $SKR token.

## Token & settlement

- **test $SKR**, one token, three jobs: the **payout** (consensus-aligned
  judgments earn $SKR), the **bond** (deposit to unlock paid judging), and the
  **slash** collateral (lose stake for systematic disagreement). A devnet
  fixture mint we control.

Settlement is **real on-chain**: the SKR bond escrows on-chain at deposit,
aligned judges are paid test $SKR on agreement (minted by the treasury), and a
systematic disagreement warns then slashes the SKR bond to a penalty vault.

## Status

October 13 2026:

- Deployed to **Solana devnet**; the judgment API is live (devnet test tokens).
- **44 backend tests** pass; **3 SBF runtime tests** are skipped because this
  clone has no compiled program artifact. Solana Seeker Kotlin compilation passes.
  The supplied Python venv needs a temporary `pydantic-core==2.46.5` overlay
  to match its installed Pydantic version.
- Payout, bond, and slash use a single test $SKR token in this source revision.
  This branding change does not build or deploy the on-chain program.
  The single-mint program is live on devnet (program
  `5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV`, upgradeable).
- Seeded with **100 judgment pairs** (60 A/B preference + 40 rating) for the demo.
- Everything here runs on **devnet test tokens**.

Not done / honest caveats:

- Liveness is an on-device motion challenge, not bank-grade anti-spoofing
  (device attestation is a roadmap item). It is present but is not yet a hard
  gate in the judging path.
- Sybil resistance is partial: the bond raises the cost of fake accounts but
  does not eliminate them.
- **Video / frame-sequence judging** is the next tier (motion and sequence
  perception) and is planned but not yet live, the demo judges still images.

## Install the app

The Solana Seeker client ships as a debug APK (devnet build, test tokens
only); the same flows are available in the web dapp.

## Repository layout

```
.
├── android/            Solana Seeker mobile app (Kotlin / Gradle)
├── backend/            FastAPI service
│   ├── seeker/         judgment API, consensus, bond, chain, DB
│   ├── migrations/     database migrations
│   └── tests/          backend test suite
├── programs/           Anchor workspaces
│   └── seeker-bounty/  on-chain SKR bond + warn/slash + batch-anchor program
├── scripts/            devnet bootstrap and demo helpers
├── docs/               API contract
└── .github/            CI workflows
```

## Quick start

Four commands from a fresh clone to a running backend (devnet, throwaway
dev-only keys, never the real verifier secret):

```bash
git clone https://github.com/chunli0x/capture.git && cd capture

# 1. PostgreSQL (Docker), loopback-only by default
docker compose up -d

# 2. venv + dependencies + dev-only keys + DB schema; writes backend/.env
./scripts/dev_bootstrap.sh

# 3. run the API (loads the generated backend/.env)
cd backend && .venv/bin/uvicorn seeker.api:production_app --factory --env-file .env

# 4. verify
curl -s http://localhost:8000/health
```

Notes:

- The backend listens on **port 8000**.
- `dev_bootstrap.sh` generates a freshly generated dev-only verifier keypair
  (`backend/verifier.dev.json`) and writes it to `backend/.env`. This is
  intentionally **not** the real verifier, the real key authorizes every
  warn/slash/anchor and is never committed. Anything that needs the real
  verifier on-chain will fail until you point `SEEKER_VERIFIER_KEY_FILE` at
  your own keypair.
- No Docker? Use SQLite instead, set `SEEKER_LOCAL_SQLITE=true` and
  `SEEKER_DATABASE_URL=sqlite:///./seeker.db`.
- The dependency set is tested on Python 3.11. If `python3` is a newer minor,
  pass `PYTHON=python3.11 ./scripts/dev_bootstrap.sh`.

## Build & run

Backend:

```bash
cd backend
.venv/bin/pip install -r requirements-seeker.txt
.venv/bin/python -m seeker.migrate
.venv/bin/uvicorn seeker.api:production_app --factory --env-file .env
```

(Or just run `./scripts/dev_bootstrap.sh`, which does all of the above and
writes `backend/.env`.)

Tests:

```bash
# backend
cd backend && .venv/bin/python -m pytest -q

# Anchor program (Rust unit tests + BPF build)
cargo test -p seeker-bounty --lib
anchor build
```
