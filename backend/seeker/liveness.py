"""Liveness sessions and the proof hash bound into the signed attestation.

The camera work happens on the phone. This module is the server side of it:
issue a challenge nobody can predict, accept one result for it, and derive the
hash that goes into the attestation the human signs over.

## What a liveness proof here does and does not establish

It establishes that *this backend* issued a fresh, random challenge, and that a
client answered it once, within a short window, for a specific task. That is
what makes the proof non-replayable and non-transferable between tasks.

It does **not** establish that a real face was in front of a real camera. The
face never leaves the phone - that is the privacy property we want and will
claim - but the consequence is that the server is taking the client's word for
the *result*. A modified build can assert success without opening the camera.

That gap is real and it is not closed by anything in this file. The mitigation
is device attestation (Play Integrity on Android), which is out of scope for
the hackathon build and belongs in the roadmap rather than in a claim. The
demo's honest sentence is the spec's: "a live person performed a motion
challenge", with the liveness decision made on-device and vouched for by our
verifier - never "cryptographic proof of humanness".

See docs/LIVENESS.md.

## Why the challenge is server-issued

If the device chose its own challenge, a recording of one successful run could
be replayed forever. The server picking it, once, at random, from a set the
client cannot enumerate ahead of time is what forces a fresh capture. The
session is single-use for the same reason: a nonce that can be spent twice is
not a nonce.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from enum import Enum

# Domain separation. Without it, a hash computed here could collide with one
# computed for some other purpose over the same bytes, and the attestation
# program's "reject a hash I have seen" check would be reasoning about the
# wrong thing.
PROOF_DOMAIN = b"capture.liveness.v1"

# How long a challenge stays answerable. Long enough to read the prompt and
# blink, short enough that a captured session is not a durable asset.
DEFAULT_TTL_SECONDS = 120.0


class Challenge(str, Enum):
    """Motion challenges the device can be asked to perform.

    More than one so a single recording does not satisfy every session, and
    each one detectable with ML Kit face landmarks rather than a bespoke model.
    """

    BLINK = "blink"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    SMILE = "smile"


class LivenessError(Exception):
    """A session could not be issued, found, or accepted."""


class SessionExpired(LivenessError):
    """The challenge was not answered inside its window."""


class SessionAlreadyUsed(LivenessError):
    """A liveness session is single-use; this one is spent."""


class UnknownSession(LivenessError):
    """No such session, or it belongs to a different contributor."""


@dataclass(frozen=True)
class LivenessSession:
    session_id: str
    contributor: str
    task_id: str
    challenge: Challenge
    nonce: bytes
    issued_at: float
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


def proof_hash(
    *, nonce: bytes, task_id: str, challenge: Challenge, result_digest: bytes
) -> bytes:
    """Derive the liveness proof hash committed into the signed attestation.

    Every input is load-bearing:

    - `nonce` ties the proof to one server-issued session, so it cannot be
      manufactured offline.
    - `task_id` ties it to one task. Without it, a single liveness run would
      mint attestations for every task a contributor completed afterwards,
      and "a human did this task" would collapse into "a human was here once".
    - `challenge` binds which motion was asked for, so a result captured for
      a blink cannot be presented as the answer to a head turn.
    - `result_digest` is whatever the device summarises its check as. It is
      opaque here on purpose: no biometric material should ever reach this
      process, and treating it as an opaque blob is what keeps that true even
      if the client changes what it computes.

    Lengths are prefixed rather than concatenated raw, so that two different
    field splits cannot produce the same preimage.
    """
    hasher = hashlib.sha256()
    for part in (
        PROOF_DOMAIN,
        nonce,
        task_id.encode("utf-8"),
        challenge.value.encode("utf-8"),
        result_digest,
    ):
        hasher.update(len(part).to_bytes(4, "big"))
        hasher.update(part)
    return hasher.digest()


class LivenessSessionStore:
    """In-memory liveness sessions.

    In-memory matches the rest of this backend's demo-scoped state. The
    consequence worth knowing: a restart invalidates outstanding challenges,
    so a contributor mid-check has to start one again. That is the right
    failure - the alternative, persisting nonces, would mean a stolen store
    hands over reusable proofs.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, LivenessSession] = {}
        self._used: set[str] = set()
        # Hashes already accepted, so a replay is refused here as well as
        # on-chain. The program is the authority, but failing early gives a
        # legible error instead of a rejected transaction.
        self._seen_hashes: set[bytes] = set()

    def issue(
        self,
        contributor: str,
        task_id: str,
        *,
        now: float | None = None,
        challenge: Challenge | None = None,
    ) -> LivenessSession:
        now = time.time() if now is None else now
        self._evict_expired(now)
        session = LivenessSession(
            session_id=secrets.token_hex(16),
            contributor=contributor,
            task_id=task_id,
            # secrets, not random: this value is the whole anti-replay story,
            # and a predictable nonce would let a proof be built in advance.
            challenge=challenge or secrets.choice(list(Challenge)),
            nonce=secrets.token_bytes(32),
            issued_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, contributor: str, session_id: str) -> LivenessSession:
        session = self._sessions.get(session_id)
        if session is None or session.contributor != contributor:
            # One message for both cases: distinguishing them would tell a
            # caller that somebody else's session id exists.
            raise UnknownSession("no such liveness session for this contributor")
        return session

    def complete(
        self,
        contributor: str,
        session_id: str,
        result_digest: bytes,
        *,
        now: float | None = None,
    ) -> bytes:
        """Accept one result for a session and return the proof hash."""
        now = time.time() if now is None else now
        session = self.get(contributor, session_id)

        if session_id in self._used:
            raise SessionAlreadyUsed("this liveness session has already been used")
        if session.is_expired(now):
            raise SessionExpired(
                f"the challenge expired {now - session.expires_at:.0f}s ago; "
                "request a new session"
            )
        # Reject a degenerate result digest (empty or all-zero bytes). A real
        # on-device ML Kit challenge produces a hash over detected landmark
        # motion; an empty/zero digest is the trivial bypass where a client
        # sends `"0" * 64` without running any challenge. This is a hard gate,
        # not proof — the server still cannot cryptographically verify the
        # motion itself (no camera stream crosses the wire), so liveness remains
        # an on-device assertion with per-wallet rate limits, as documented.
        if len(result_digest) == 0 or result_digest == b"\x00" * len(result_digest):
            raise LivenessError("result_digest is degenerate (empty or all-zero)")

        digest = proof_hash(
            nonce=session.nonce,
            task_id=session.task_id,
            challenge=session.challenge,
            result_digest=result_digest,
        )
        if digest in self._seen_hashes:
            raise SessionAlreadyUsed(
                "this liveness proof has already been used for an attestation"
            )

        # Marked used before the caller does anything with the hash: a failure
        # downstream must not leave a spendable session behind.
        self._used.add(session_id)
        self._seen_hashes.add(digest)
        return digest

    def has_seen(self, digest: bytes) -> bool:
        return digest in self._seen_hashes

    def _evict_expired(self, now: float) -> None:
        """Drop expired sessions, but never forget a spent hash.

        Expiry frees memory; the seen-hash set is the replay guard and must
        outlive the session it came from.
        """
        expired = [k for k, v in self._sessions.items() if v.is_expired(now)]
        for key in expired:
            del self._sessions[key]
            self._used.discard(key)
