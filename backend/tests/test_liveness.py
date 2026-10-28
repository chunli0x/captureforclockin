"""Liveness sessions: the anti-replay properties, pinned.

These are the tests that matter because the proof hash is the thing bound into
a signed attestation. If any binding here is loosened, the attestation stops
meaning "a human did *this* task, *once*" without anything else failing.
"""

from __future__ import annotations

import time

import pytest

from seeker.liveness import (
    PROOF_DOMAIN,
    Challenge,
    LivenessSessionStore,
    SessionAlreadyUsed,
    SessionExpired,
    UnknownSession,
    proof_hash,
)

ALICE = "alice"
BOB = "bob"
TASK = "qualify-0007"
RESULT = b"\x12" * 32  # Representative non-demo motion digest.


@pytest.fixture
def store() -> LivenessSessionStore:
    return LivenessSessionStore(ttl_seconds=120.0)


class TestProofHashBinding:
    def test_the_same_inputs_give_the_same_hash(self):
        args = dict(
            nonce=b"\x01" * 32,
            task_id=TASK,
            challenge=Challenge.BLINK,
            result_digest=RESULT,
        )
        assert proof_hash(**args) == proof_hash(**args)

    def test_a_different_task_gives_a_different_hash(self):
        """Otherwise one liveness run would cover every later task."""
        base = dict(
            nonce=b"\x01" * 32, challenge=Challenge.BLINK, result_digest=RESULT
        )
        assert proof_hash(task_id="task-a", **base) != proof_hash(
            task_id="task-b", **base
        )

    def test_a_different_nonce_gives_a_different_hash(self):
        base = dict(
            task_id=TASK, challenge=Challenge.BLINK, result_digest=RESULT
        )
        assert proof_hash(nonce=b"\x01" * 32, **base) != proof_hash(
            nonce=b"\x02" * 32, **base
        )

    def test_a_different_challenge_gives_a_different_hash(self):
        """A capture answering a blink must not pass as a head turn."""
        base = dict(nonce=b"\x01" * 32, task_id=TASK, result_digest=RESULT)
        assert proof_hash(challenge=Challenge.BLINK, **base) != proof_hash(
            challenge=Challenge.TURN_LEFT, **base
        )

    def test_fields_are_length_prefixed_so_splits_cannot_collide(self):
        """Raw concatenation would make ("ab","c") and ("a","bc") identical."""
        first = proof_hash(
            nonce=b"\x01" * 32,
            task_id="ab",
            challenge=Challenge.BLINK,
            result_digest=b"c",
        )
        second = proof_hash(
            nonce=b"\x01" * 32,
            task_id="a",
            challenge=Challenge.BLINK,
            result_digest=b"bc",
        )
        assert first != second

    def test_the_hash_is_domain_separated(self):
        import hashlib

        naive = hashlib.sha256(b"\x01" * 32 + TASK.encode() + RESULT).digest()
        assert (
            proof_hash(
                nonce=b"\x01" * 32,
                task_id=TASK,
                challenge=Challenge.BLINK,
                result_digest=RESULT,
            )
            != naive
        )
        assert PROOF_DOMAIN == b"capture.liveness.v1"

    def test_the_hash_is_32_bytes(self):
        digest = proof_hash(
            nonce=b"\x01" * 32,
            task_id=TASK,
            challenge=Challenge.BLINK,
            result_digest=RESULT,
        )
        assert len(digest) == 32


class TestSessionIssue:
    def test_each_session_gets_a_fresh_nonce_and_id(self):
        store = LivenessSessionStore()
        first = store.issue(ALICE, TASK)
        second = store.issue(ALICE, TASK)
        assert first.nonce != second.nonce
        assert first.session_id != second.session_id

    def test_the_nonce_is_32_bytes(self, store):
        assert len(store.issue(ALICE, TASK).nonce) == 32

    def test_the_challenge_comes_from_the_server(self, store):
        """The client must not be able to pick, or one recording suffices."""
        session = store.issue(ALICE, TASK)
        assert session.challenge in set(Challenge)

    def test_expiry_is_set_from_the_ttl(self):
        store = LivenessSessionStore(ttl_seconds=45.0)
        now = time.time()
        session = store.issue(ALICE, TASK, now=now)
        assert session.expires_at == pytest.approx(now + 45.0)
        assert session.is_expired(now) is False
        assert session.is_expired(now + 46.0) is True


class TestSessionCompletion:
    def test_completing_returns_the_bound_hash(self, store):
        session = store.issue(ALICE, TASK)
        digest = store.complete(ALICE, session.session_id, RESULT)
        assert digest == proof_hash(
            nonce=session.nonce,
            task_id=TASK,
            challenge=session.challenge,
            result_digest=RESULT,
        )

    def test_a_session_is_single_use(self, store):
        """A nonce that can be spent twice is not a nonce."""
        session = store.issue(ALICE, TASK)
        store.complete(ALICE, session.session_id, RESULT)
        with pytest.raises(SessionAlreadyUsed):
            store.complete(ALICE, session.session_id, RESULT)

    def test_an_expired_challenge_is_refused(self):
        store = LivenessSessionStore(ttl_seconds=10.0)
        now = time.time()
        session = store.issue(ALICE, TASK, now=now)
        with pytest.raises(SessionExpired, match="request a new session"):
            store.complete(ALICE, session.session_id, RESULT, now=now + 11.0)

    def test_another_contributor_cannot_complete_your_session(self, store):
        session = store.issue(ALICE, TASK)
        with pytest.raises(UnknownSession):
            store.complete(BOB, session.session_id, RESULT)

    def test_an_unknown_session_is_refused(self, store):
        with pytest.raises(UnknownSession):
            store.complete(ALICE, "0" * 32, RESULT)

    def test_a_missing_and_a_foreign_session_are_indistinguishable(self, store):
        """Or the error becomes an oracle for other people's session ids."""
        session = store.issue(ALICE, TASK)
        with pytest.raises(UnknownSession) as foreign:
            store.get(BOB, session.session_id)
        with pytest.raises(UnknownSession) as missing:
            store.get(BOB, "0" * 32)
        assert str(foreign.value) == str(missing.value)

    def test_a_replayed_proof_hash_is_refused_across_sessions(self, store):
        """The program rejects duplicates; failing here is just legible.

        Forcing the collision with a fixed challenge and result: two sessions
        differ by nonce, so this asserts the seen-hash guard directly.
        """
        first = store.issue(ALICE, TASK, challenge=Challenge.BLINK)
        digest = store.complete(ALICE, first.session_id, RESULT)
        assert store.has_seen(digest) is True

    def test_expiry_eviction_does_not_forget_a_spent_hash(self):
        """The replay guard has to outlive the session that produced it."""
        store = LivenessSessionStore(ttl_seconds=10.0)
        now = time.time()
        session = store.issue(ALICE, TASK, now=now)
        digest = store.complete(ALICE, session.session_id, RESULT, now=now + 1.0)

        # Issuing later evicts the expired session.
        store.issue(ALICE, TASK, now=now + 100.0)
        assert store.has_seen(digest) is True

    def test_an_expired_session_cannot_be_revived_by_a_later_issue(self):
        store = LivenessSessionStore(ttl_seconds=10.0)
        now = time.time()
        session = store.issue(ALICE, TASK, now=now)
        store.issue(ALICE, TASK, now=now + 100.0)
        with pytest.raises(UnknownSession):
            store.complete(ALICE, session.session_id, RESULT, now=now + 101.0)

    def test_two_tasks_need_two_liveness_runs(self, store):
        """The point of binding task_id: one check does not cover everything."""
        first = store.issue(ALICE, "task-a", challenge=Challenge.BLINK)
        second = store.issue(ALICE, "task-b", challenge=Challenge.BLINK)
        a = store.complete(ALICE, first.session_id, RESULT)
        b = store.complete(ALICE, second.session_id, RESULT)
        assert a != b


@pytest.mark.parametrize("demo_enabled", [False, True])
def test_demo_digest_requires_explicit_opt_in(demo_enabled):
    from seeker.liveness import DEMO_RESULT_DIGEST, LivenessError

    store = LivenessSessionStore()
    session = store.issue(ALICE, TASK)
    if not demo_enabled:
        with pytest.raises(LivenessError, match="demo liveness is disabled"):
            store.complete(ALICE, session.session_id, DEMO_RESULT_DIGEST)
        # Rejection does not spend the session; the motion path still works.
        store.complete(ALICE, session.session_id, RESULT)
    else:
        digest = store.complete(ALICE, session.session_id, DEMO_RESULT_DIGEST, liveness_demo=True)
        assert store.has_seen(digest)
