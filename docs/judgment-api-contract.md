# Capture Judgment, Wire Contract (single source of truth)

Status: 2026-10-12. PM-authored. Every builder (backend, blockchain-dev, front-end)
reads THIS file and implements to it. Do not invent different field names or paths.
If a field here is wrong, change it HERE and re-read; do not fork the contract.

Product = Capture. Device = Solana Seeker. Currency = test $SKR (devnet, not real value).
Designed for the Solana Seeker mobile. I sign in with Seeker Wallet to stake and judge.
Public copy stays strictly product-focused ("judge a vision model's outputs for
RLHF"); do not disclose the underlying training use. Majority-preference bias
must be stated, never hidden. "can't tell" is first-class and never penalized.

Pinned identifiers (devnet):
- program id:  5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV  (seeker-bounty)
- skr mint:   3aGKaVL7GpcTcTuKjkyirnVeyNG1JGFNyGGueu8mAGbM   (devnet TEST $SKR, payout + bond + slash)


---

## 1. Constants (tunable; these are the devnet defaults)

- K = 3 (judges per A/B pair). Config `judge_k`.
- Agreement rolling window = 100 resolved judgments. Config `judge_window`.
- Judge ladder thresholds (agreement_rate): 0.80 / 0.88 / 0.93 / 0.97
  (levels 1..4). See §6.
- Slash floor: rolling agreement_rate < 0.45 AND judged_count >= 20 ->
  trigger two-strike warn/slash. Config `judge_slash_floor`, `judge_slash_min_judged`.
- Payout ($SKR base units, 6 decimals, config-backed): `judge_base_skr` (per aligned
  judgment), `judge_flat_skr` (flat "can't tell"). Test $SKR uses 6 decimals and has no real value.
  Devnet defaults: `judge_base_skr = 20_000` (= 0.02 $SKR/aligned judgment),
  `judge_flat_skr = 5_000` (= 0.005 $SKR flat). Level multipliers (§6)
  apply on top of base. These are starting hypotheses per incentive spec §8,
  tune against real agreement data.

## 2. Data model (exact field names)

New tables:

judgment_pair:
  pair_id (str, pk), batch_id (str, fk->judgment_batch), media_type ("image"|"video"),
  task_type ("preference"|"rating"), output_a_url (str|null), output_b_url (str|null),
  rubric (str|null), k (int, default 3), status ("open"|"collecting"|"resolved"|"rerouted"),
  preference (str|null: "a"|"b"), agreement (float|null), confidence (float|null),
  created_at (int)

judgment:
  judgment_id (str, pk), pair_id (str, fk), wallet (str, fk->Actor),
  choice ("a"|"b"|"tie"|"cant_tell"|"rating"), rating (int|null, 1..5),
  aligned (bool|null), submitted_at (int)
  UNIQUE(pair_id, wallet)

calibration_pair:
  pair_id (str, pk), known_choice ("a"|"b"|"rating_value"), known_consensus (float),
  media_type, task_type, output_a_url, output_b_url, rubric

judgment_batch:
  batch_id (str, pk), merkle_root (str|null), created_at (int)

Extend Actor (NOT a separate judge table, matches the existing denormalization):
  calibrated (bool, default false), agreement_rate (float, default 0.0),
  judged_count (int, default 0), staked_usdc (int, default 0), warnings (int, default 0)
  (calibration progress is derived from calibration_pair rows answered, not a column.)

The stored columns `Actor.usdc_funded`, `Actor.staked_usdc`, and
`Judgment.earned_usdc` retain their legacy names to avoid a migration. They
now record test $SKR funding and amounts. The API exposes `skr_funded`,
`staked_skr`, and `earned_skr`; `settled_skr` is computed from confirmed payouts.
Existing migration mappings, including the legacy `settled_usdc` name, stay unchanged.

## 3. API surface

Auth = Bearer token from /auth/challenge -> /auth/verify (unchanged).

GET /me
  - "bond_test_skr_units": minimum bond in test $SKR base units (6 decimals)
  - "skr_funded": faucet funding flag, "staked_skr": recorded stake

GET /judge/next
  -> 200 if a task is available; 404 if none. Body:
  {
    "kind": "judgment",                    // judgment pairs only (calibration removed)
    "pair_id": "...",
    "task_type": "preference" | "rating",
    "media_type": "image" | "video",
    "output_a_url": "..." | null,         // null for rating
    "output_b_url": "..." | null,
    "rubric": "..." | null,
    "calibrated": bool,
    "calibration_remaining": int          // >0 when still calibrating
  }

POST /judge/{pair_id}
  body: {"choice": "a"|"b"|"tie"|"cant_tell"|null, "rating": 1..5|null}
  (preference -> choice required; rating -> rating required)
  -> 200 {"status": "submitted"} on a non-resolving submit, or
     {"status": "resolved", "aligned": bool, "earned_skr": int, "preference": "a"|"b"|null}
  (idempotent: same (pair_id,wallet) already answered returns 409; re-submit of the
   exact same answer returns 200 with current status, not a double pay.)
  "cant_tell" -> pair rerouted (status "rerouted", k incremented), flat_skr earned,
  never penalized, never forced to guess.

GET /judge/stats
  {
    "calibrated": bool,
    "calibration_progress": {"completed": int, "required": 10, "correct": int},
    "agreement_rate": float,
    "judged_count": int,
    "level": int, "level_name": "Scout",
    "rate_multiplier": float,
    "staked_skr": int,
    "warnings": int
  }

POST /bond/faucet  (exists, mints test $SKR on-chain, idempotent)
POST /bond/deposit (exists, real deposit_bond escrow; sets bonded+worker on confirm)
POST /bond/withdraw (exists, real withdraw_bond; clears bonded on confirm)

GET /batch/{id}/dataset
  {
    "batch_id": "...",
    "merkle_root": "hex" | null,
    "anchored": false,
    "entries": [
      {"pair_id","output_a_url","output_b_url","preference","agreement","confidence",
       "media_type","task_type","rubric"}
    ]
  }

/health additions (MUST be present):
  - "skr_mint", "program_id",
    "treasury" ($SKR mint-authority/treasury pubkey),
    "slash_authority" (verifier pubkey), "skr_payout": "ready"|"unavailable",
    "test_tokens": true, "dataset_bias": "majority-preference".

## 4. Consensus scoring (backend owns)

On judgment submit, when a pair reaches K judgments:
1. "cant_tell" is neutral: no vote, pair -> "rerouted", k += 1 (more judges).
   flat_skr to the "cant_tell" judge.
2. Majority over a/b. Tie/even split -> "rerouted", k += 1.
3. Rating: median; aligned = |rating - median| <= 1.
4. Pay aligned judges judge_base_skr * level_multiplier ($SKR). Dissenters get 0.
5. Record preference (majority), agreement = aligned/voting, confidence =
   agreement * min(1.0, voting/k). Mark pair "resolved".
agreement_rate = rolling window (last 100 resolved) fraction aligned.

## 5. Calibration (removed as a gate)

The calibration gate is removed: a wallet can /bond/deposit and judge without
passing calibration. The calibration_pair/calibration_answer tables and the
`calibrated` field remain dormant (kept for data continuity, not enforced).

## 6. Judge ladder (new module judge_leveling.py; DO NOT touch leveling.py)

| level | name      | agreement_rate | multiplier |
|-------|-----------|----------------|------------|
| 0     | Scout     | no gate        | 1.00       |
| 1     | Regular   | >= 0.80        | 1.10       |
| 2     | Veteran   | >= 0.88        | 1.25       |
| 3     | Expert    | >= 0.93        | 1.50       |
| 4     | Pathfinder| >= 0.97        | 2.00       |

Payout = judge_base_skr * multiplier.

## 7. On-chain $SKR payout (backend owns; chain.py)

Add `chain.settle_skr(wallet, amount)`, pays amount $SKR to the judge's $SKR
ATA, server-signed. The treasury keypair is the $SKR mint authority; prefer a
transfer from the treasury's $SKR ATA, and if the treasury holds no $SKR balance,
mint_to the payout (mint authority) so the demo never deadlocks on a funding
step. Fail closed (ChainUnavailable -> 503) and never record earnings without a
confirmed signature. Reuse the settle durability pattern (reserve -> broadcast ->
confirm -> clear) so a lost tx cannot lose or double-pay.

## 8. Merkle provenance, backend computes, on-chain anchor deferred

The backend computes a Merkle root over each resolved batch and records it on the
`judgment_batch.merkle_root` column. The `anchored` flag marks whether that root
has been committed on-chain.

The on-chain commit is the `post_batch` instruction (blockchain-dev owns the
Rust; backend wires chain.py), **deferred** because the currently deployed
program is immutable (upgrade authority burned) and predates the `Batch` account.
The instruction is specified here so the next program deploy can add it:

New program instruction `post_batch(batch_id: [u8;16], merkle_root: [u8;32])`.
Anchor discriminator = sha256("global:post_batch")[:8].
data = discriminator || batch_id (16 bytes LE/raw) || merkle_root (32 bytes).
accounts (in order):
  1. authority: Signer (must == config.slash_authority; the verifier key)
  2. config: Account<Config>, seeds=["config"], bump
  3. batch: Account<Batch>, init, payer=authority, seeds=["batch", batch_id], bump
  4. system_program
  5. rent

New account struct:
  #[account] pub struct Batch { pub merkle_root: [u8;32] }   // space = 8 + 32

`init` makes it append-only (a batch_id can only be anchored once).

chain.py build path: action "post_batch", accounts = [verifier(signer), config,
batch_pda(init), SYSTEM, RENT], data = discriminator("post_batch") + batch_id(16)
+ merkle_root(32). Server-signed (verifier = slash_authority). `post_batch`
currently fails closed until the new program binary is deployed.

Merkle root (backend computes): over resolved pairs in a batch, leaf =
sha256(f"{pair_id}|{preference}|{agreement}|{confidence}"), standard Merkle tree,
pad the last leaf to a power of two; root = top hash (hex). Simple commitment;
document it in README as a hackathon-grade provenance anchor, tunable.

## 9. Hard rules (both sides)

- Honest devnet: "test $SKR (payout + bond)" in UI and /health.
- Majority-preference bias stated in README.
- "can't tell" first-class, never penalized.
- Public copy stays strictly product-focused ("judge a vision model's outputs for
  RLHF"); do not disclose the underlying training use.
- Secrets never committed; .env/keypairs gitignored + 0600.
- Do NOT delete capture code (judge.py, leveling.py, payout.py, capture endpoints);
  leave dormant; the judgment flow must NOT call judge.py.

## 10. Seeding (demo data)

Pairs/calibration/batches are loaded by an operator seed script
(scripts/seed_judgment.py), not a public endpoint. It must insert >= 10
calibration pairs with known choices, plus judgment pairs across >= 1 batch.
