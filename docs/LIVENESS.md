# Liveness policy and attestation plumbing

`SEEKER_LIVENESS_DEMO` defaults to `false`. The exact 32-byte `deadbeef` repeated
8 times digest is rejected with `demo liveness is disabled; complete the motion
check`. Android reads `liveness_demo` from `/me` and shows Continue (demo) only
when true; a missing flag defaults to false. The camera/motion path is unchanged.
For the hackathon, the PM can explicitly set `SEEKER_LIVENESS_DEMO=true`.

Migration **0018** adds `liveness_verified_at`, `liveness_method`,
`liveness_expires_at` and `liveness_device_integrity` to `seeker_actors`.
Timestamps are UTC Unix seconds. No prior Boolean is promoted into evidence.
The old column is retained solely for additive migration compatibility.

`/liveness/complete` records a server-selected method (`demo`, `motion`, or
`attestation`), completion time and expiry. The lifetime defaults to one hour,
configured with positive `SEEKER_LIVENESS_VERIFICATION_SECONDS`. A verified
device provider's shorter expiry caps this lifetime. Completion returns the
actual session task ID, bound proof hash and current liveness fields.

`/me` reports the method, timestamps, device-integrity signal and
`liveness_fresh`. A record is fresh only when `verified_at <= now < expires_at`
and its method is recognized. Demo records also require demo mode to remain on.
Both fetching new judgment work and submitting a new judgment enforce freshness.
Already accepted judgment retries retain their existing idempotency behavior.
Expired records remain visible for explanation, but do not unlock work.

`liveness_completed` in the API is a compatibility field derived from freshness
(or the explicit `SEEKER_LIVENESS_REQUIRED=false` gate override). The separate
`liveness_fresh` field never claims verification because the gate is disabled.
Paid operation requires `SEEKER_LIVENESS_REQUIRED=true` and demo mode off.
`/health` also reports both configuration flags.

## Provider boundary

`seeker.attestation.verify_wallet_proof` verifies the existing wallet-signed
sign-in challenge, including expiry. Liveness completion still requires that
unexpired authenticated wallet session and matching contributor. The client
does not need another wallet prompt and cannot choose its recorded method.
Wallet ownership is not evidence of a face or motion.

`create_app(..., attestation_verifier=AttestationVerifier(provider))` accepts a
`DeviceIntegrityVerifier` implementation. Its `verify` method receives the
wallet, server-derived proof hash, optional `device_integrity_token`, and server
time. The proof hash retains the existing domain + nonce + task + challenge +
result binding. A provider must verify token authenticity, intended app/package
and signing certificate, wallet and proof binding, freshness and replay policy.
It returns `DeviceIntegrity(signal, expires_at)` or raises `AttestationError`.
Network/provider failures must fail closed, never silently become successful
motion checks. Raw device tokens are not persisted.

The default provider returns signal `none` and rejects supplied device tokens
because it cannot verify them. A valid provider signal yields `attestation` for
a non-demo result; a demo result always remains `demo`. Missing/expired provider
expiry is rejected. Failed provider verification cannot create a record and
requires a new single-use liveness session.

**Play Integrity is not implemented.** Without a real provider, `motion` means
the authenticated client reports a completed on-device challenge. Rejecting the
known demo digest does not prevent a modified client from forging another
nonzero digest. No camera data is uploaded. Outstanding challenge sessions
remain process-local and are invalidated on backend restart; accepted
verification records persist in the database until expiry.

## Checks

From each repo's `backend/`:

```sh
SEEKER_LOCAL_SQLITE=true .venv/bin/python -m pytest -q
```

From each repo's `android/`:

```sh
./gradlew :app:testDebugUnitTest --console=plain
```

Tests cover demo opt-in/rejection, expiry boundaries, both judgment gates,
record persistence across app recreation, legacy migration, wallet signatures,
and injected valid/expired device-integrity signals. Provider tests use a fake
verifier; they do not claim real device attestation or physical-device coverage.
