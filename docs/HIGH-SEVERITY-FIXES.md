# Capture SKR security fixes and operator rollout

These changes apply only to Capture SKR. No program deployment is performed by
building or testing this repository.

## Program and backend

The new program is required for fixes 2, 3, 4, and 6. `record_payment` now takes
payment ID, recipient, and amount, verifies the mint authority of the configured
SKR mint, and performs the mint CPI itself. Its `payment-v2` PDA binds mint,
recipient, amount, and ID. Do not append a second SPL mint instruction. Config
and Bond account layouts and the program address are unchanged. Existing
`payment` PDAs remain untouched.

`reserve_judgment` creates one liability PDA per worker and judgment ID, checks
the funded bond, and increments `pending` atomically. `resolve_judgment` records
one immutable enforcement result, atomically warns/slashes when required, and
releases that liability. A cancellation can create a resolved tombstone before
a delayed reservation lands. Replays cannot increment, release, warn, or slash
twice. Depositing into a slashed bond cannot erase remaining liabilities.

The backend persists submission IDs before reservation and signed payout bytes,
signatures, and expiry heights before broadcasting. Only reserved votes count
toward consensus. Payout retries verify the exact finalized receipt and locate
the matching finalized transaction. A legacy receipt alone is insufficient:
its original successful transaction must match the exact mint instructions.
Mismatched/poisoned legacy receipts require operator investigation; the backend
fails closed rather than risking a second mint.

Statistics and enforcement decisions are journaled together, using the unique
judgment ID as the resolution ID. Pairs remain `enforcing` after a failed
warning/slash/release. Reposting the same judgment retries pending work outside
the submission transaction, including after that judge has been slashed. Neutral
payouts/releases are also retryable. On-chain results, not the DB mirror, prevent
duplicate effects after transport errors or DB commit failures.

For an existing installation, the operator should:

1. Stop the old API and let its in-flight transactions finalize before upgrading.
2. Back up the database and upgrade the program using the existing program ID
   and the operator's intended program/upgrade-authority keys.
3. Apply schema changes with `cd backend && python -m seeker.migrate`.
4. While the API is stopped, run `python -m seeker.liabilities` with the normal
   backend environment. **This command writes on-chain.** It reserves existing
   accepted, unresolved votes. It is idempotent. If a legacy bond was already
   withdrawn/slashed, resolve that historical liability operationally before
   reopening; do not mark it reserved without an on-chain reservation.
5. Start the updated API. Pending settlement/enforcement can be reconciled by
   reposting the original judgment. The API also backfills legacy reservations
   when a pair is accessed, but backfilling before reopening closes the upgrade
   window for all outstanding votes.

Old pairs already marked resolved have no historical enforcement journal. Do
not blindly replay their penalties; review them against their chain history.
This change cannot reconstruct whether the old code swallowed an enforcement
failure. No existing resolved records are silently given new penalties.

## Android distribution

Debug defaults still support the emulator. Every release task depends on
`validateReleaseDistribution`: an explicit HTTPS API base is required, with no
loopback/emulator host, URL credentials, query, or fragment. DNS and TLS must
work and `<base>/health` must return HTTP 2xx without a redirect. Network failure
blocks distribution rather than producing an APK pointing at an unreachable
backend.

Copy `android/signing.properties.example` to the ignored
`android/signing.properties` and supply the persistent release keystore path,
store password, alias, and key password. Reuse and back up the same keystore for
future updates. No production keystore is generated or committed here.
Alternatively set `SEEKER_KEYSTORE_FILE`, `SEEKER_KEYSTORE_PASSWORD`,
`SEEKER_KEY_ALIAS`, and `SEEKER_KEY_PASSWORD` in the build environment.

Build with the operator's reachable backend, for example:

```sh
cd android
./gradlew assembleRelease -PseekerApiBase=https://capture.example.org
```

`SEEKER_API_BASE` can supply the URL instead. Release signs with the persistent
`release` signing config. Missing credentials block the build.

Anti-spoof fusion requires five consecutive recent live frames. A spoof frame
breaks the run and restarts motion; sustained spoof evidence takes precedence
and remains latched until a new attempt. Face loss, ambiguous/missing tracking,
failed classification, and gaps over 750 ms reset evidence and motion together.
The existing classifier-unavailable fallback is unchanged; server-side device
attestation remains outside this change.

## Verification (2026-10-14)

- Backend: `SEEKER_LOCAL_SQLITE=true /home/hermes/tap-ci/backend/.venv/bin/python -m pytest -q`
  from `backend/`: **63 passed**, including compiled SBF/SPL CPI execution in
  LiteSVM, concurrent retries, transport failures, and migration coverage.
- Rust: `cargo test -p seeker-bounty --lib`: **7 passed**.
- `anchor build`: **succeeded**. The existing ignored local deployment keypair
  differs from the declared program ID. Use the intended program and upgrade
  authority keys for rollout; do not change the source identity merely to
  silence this warning. The built SBF executes under the declared ID in LiteSVM.
- Android: `ANDROID_HOME=/home/hermes/android-sdk JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew testDebugUnitTest --console=plain`
  from `android/`: **64 passed**.
- Release validation: **5 checks passed**: missing URL, HTTP/emulator URL,
  missing persistent signing credentials, and unreachable HTTPS were rejected;
  a reachable HTTPS health endpoint with a temporary persistent test keystore
  was accepted. Test keys were removed afterward. No production signing keys
  or deployment were used.
