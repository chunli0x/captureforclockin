# Transfer payouts (token workstream, step 1)

Implemented for test environments only. No mint or vault has been selected for
mainnet. **A real mint/vault selection and independent program review are required
before any mainnet deploy.** This change does not enable mainnet configuration,
deploy a program, fund a vault, push code, or restart a service.

## Program contract

Existing program IDs, `Config`/`Payment` layouts, `record_payment` instruction,
and `payment-v2` receipt derivation are unchanged. The mint path still mints;
backend configuration selects which payout path to use. The v3 pause and caps
apply to the transfer vault, not the legacy mint authority.

New accounts and instructions:

- `PayoutVault`: policy PDA `["payout", config]`, 137 bytes including discriminator.
  It owns the SPL token vault `["payout-vault", payout_policy]`.
- `initialize_payout(payout_authority, per_transfer_cap, epoch_cap)`: config admin
  creates the policy and an **empty, paused** vault for the configured test mint.
  Funding is a separate operation; initialization never mints or transfers funds.
- `configure_payout(payout_authority, paused, per_transfer_cap, epoch_cap)`: config
  admin rotates the payout signer, pauses/enables transfers, or changes caps.
  The authority must differ from admin, verifier, slash authority, and the current
  upgrade authority; the ProgramData account is checked against the program.
  Updating policy never resets spending already recorded in the current epoch.
- `record_payment_v3(payment_id: [u8;16], recipient: Pubkey, amount: u64,
  obligation_version: u64, valid_until_slot: u64)`: payout authority signs and pays
  transaction/account rent; the policy PDA signs the token CPI. The beneficiary
  does not sign. Amount is in base units, and version must be positive.
- `PaymentV3`: receipt PDA `["payment-v3", payment_id]`, 136 bytes. Fields in wire
  order: recipient, mint, amount, payment ID, funding vault, obligation version.
  Neither recipient, amount, mint, vault, signer nor version participates in PDA
  derivation. Both exact duplicates and conflicting retries fail `init`; the
  backend reconciles exact successful receipts without broadcasting again.

Transfers use legacy SPL Token `transfer_checked`, with a fixed Token program,
checked mint/owner on both token accounts, a six-decimal mint, a PDA-derived
source vault, and a destination distinct from the source. Token-2022 is not
supported. A mint authority is neither read nor required. See the
[Solana transfer contract](https://solana.com/docs/tokens/basics/transfer-tokens).

Pause, per-transfer caps and epoch caps are checked on-chain. Epoch means
`Clock.epoch` (Solana epochs, not wall-clock days). Spending resets only when a
successful transfer observes a newer epoch; checked arithmetic prevents overflow.
Failed transfers roll back receipt creation and spending. Attempt expiry uses
`Clock.slot <= valid_until_slot` and is not part of the immutable obligation.
Receipts cannot be closed or reset through this program.

## Backend configuration

| Setting | Meaning |
| --- | --- |
| `SEEKER_PAYOUT_MODE` | `mint` (default) or `transfer` |
| `SEEKER_PAYOUT_VAULT` | Public address of the derived, prefunded test token vault |
| `SEEKER_PAYOUT_SIGNER` | Public address of the separate configured payout authority |
| `SEEKER_PAYOUT_SIGNER_KEY_FILE` | Worker-only private key path; never read by the API |
| `SEEKER_PAYOUT_ATTEMPT_SLOTS` | Attempt lifetime, default 300; allowed 1–1200 |

The existing configured token mint remains the payout mint. No real mint address
is supplied here. Transfer mode requires the public vault and signer; worker
readiness additionally requires the corresponding key. `backend/payout-signer.json`
is ignored by git. A treasury/mint-authority key is not required for transfer
payouts; the legacy faucet remains a separate feature requiring its treasury key.

`chain.py` builds public instructions and checks environment/policy. Signing stays
in worker-only `signing_chain.py`. `Settlement._payout` chooses `settle_transfer`
or the existing mint method and reuses the same fenced durable transaction,
signature, block-height journal and outbox/reconciler. No schema migration is
needed. Judgment obligations currently use immutable version 1; lease fencing
versions are not payment versions.

The journal commits before broadcast. Retry verifies the signed transaction and
all obligation fields **before** considering expiry. A live attempt reuses exact
bytes. After blockhash or slot expiry, the worker journals a fresh attempt for
the same receipt. Finalized receipts must match every byte. Reconciliation also
checks a finalized signed transaction, permitting its original deadline and
historically authorized signer to differ from the replacement attempt. This
handles a late original transaction and signer rotation without inventing a
signature or paying twice.

Mode changes do not migrate unresolved obligations. A journal of the other mode
fails closed even after expiry; known receipts from the other namespace also
block payment. Drain/reconcile pending work under its original mode before a
mode switch. Legacy v2 identity remains its historical tuple; v3 introduces the
single-ID guarantee for transfer payments, not a retroactive v2 migration.

## Verification (no RPC writes or live funds)

Run in each repository with Rust and `cargo-build-sbf` installed:

```sh
cargo test -p seeker-bounty --lib
cd backend
SEEKER_LOCAL_SQLITE=true .venv/bin/python -m pytest -q
```

The Rust host suite builds current SBF into `target/transfer-test` once per run,
then executes it in LiteSVM against synthetic test accounts and a prefunded
1,000-unit vault. The test mint has **no mint authority**. It covers conservation
and unchanged supply, immutable receipts, duplicate/conflicting IDs, incorrect
vault/mint/signer/destination/token program/decimals, pause and spending caps,
epoch rollover, empty-vault atomic failure, expiry/failover, initialization,
admin controls and signer rotation.

Backend tests use devnet configuration with an in-memory RPC and ephemeral test
keys, and execute an actual signed backend transfer against the compiled SBF in
LiteSVM. They cover exact receipt fields, durable persistence, byte-identical
retries, expiry, late originals, rotation, cross-mode refusal, confirmation loss,
and outbox process-death windows. The backend VM test requires the Rust build
above; CI's program job builds it before running that test.

This is local VM/test verification, **not a public-devnet deployment or live
transaction test**. A live smoke test is intentionally not performed under this
task's no-deployment/no-spending restriction.
