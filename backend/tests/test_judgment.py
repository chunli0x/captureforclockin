"""Judgment product (RLHF preference judging) — consensus, calibration, ladder,
$SKR payout, reroute, and the agreement-floor slash trigger."""

import base64
import hashlib
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from seeker.api import create_app
from seeker.config import Settings
from seeker.db import Job, Actor, CalibrationPair, Judgment, JudgmentBatch, JudgmentPair
from seeker.chain import ChainUnavailable
from seeker.judge_leveling import level_for, rate_multiplier
from seeker import judgment as judgment_mod
from solders.keypair import Keypair


class FakeChain:
    def __init__(self):
        self.prepared = []
        self.settled_skr = []  # (wallet, amount) from settle_skr
        self.issued = []       # (action, wallet, amount) from issue
        self.posted = []       # (batch_id, root) from post_batch
        self.warnings = 0
        self.worker_warnings = {}
        self.reservations = set()
        self.resolutions = {}
        self.payments = {}

    def height(self):
        return 10

    def prepare(self, action, wallet, bounty=None, submission=None):
        self.prepared.append((action, wallet))
        return {"transaction": "TEST-ONLY", "message_hash": "a" * 64, "last_valid_height": 100}

    def confirm(self, p, sig):
        return True

    def find_signature(self, p, *args):
        return str(Keypair().sign_message(p.id.encode()))

    def bond_warnings(self, wallet):
        return self.worker_warnings.get(wallet, self.warnings)

    def bond_vault_balance(self, wallet):
        return 1_000_000  # >= min_bond; test judges are assumed bonded

    def issue(self, action, wallet, amount=None):
        self.issued.append((action, wallet, amount))
        return str(Keypair().sign_message(f"issue-{len(self.issued)}".encode()))

    def reserve_judgment(self, wallet, resolution_id):
        assert resolution_id not in self.resolutions
        self.reservations.add((wallet, resolution_id))

    def resolve_judgment(self, wallet, resolution_id, penalize):
        if resolution_id not in self.resolutions:
            self.resolutions[resolution_id] = penalize
            warnings = self.bond_warnings(wallet)
            if penalize and warnings < 2:
                self.issued.append(("warn", wallet, None))
                warnings += 1
                if warnings == 2:
                    self.issued.append(("slash", wallet, None))
                    warnings = 255
            self.worker_warnings[wallet] = warnings
            self.reservations.discard((wallet, resolution_id))
        else:
            assert self.resolutions[resolution_id] == penalize
        return self.bond_warnings(wallet)

    def settle_skr(self, wallet, amount, memo=None, *, persist, prepared=None):
        if memo not in self.payments:
            sig = str(Keypair().sign_message(memo.encode()))
            persist({"transaction": "SIGNED-TEST-TX", "signature": sig, "last_valid_height": 100})
            self.payments[memo] = sig
            self.settled_skr.append((wallet, amount))
        return self.payments[memo]

    def faucet(self, wallet, *, persist, prepared=None):
        return str(Keypair().sign_message(b"faucet"))

    def post_batch(self, batch_id, merkle_root):
        self.posted.append((batch_id, merkle_root))
        return str(Keypair().sign_message(b"batch"))


@pytest.fixture
def pilot_database_url(tmp_path):
    url = os.environ.get("SEEKER_TEST_DATABASE")
    if not url:
        yield "sqlite:///" + str(tmp_path / "judge.db")
        return
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    parsed = make_url(url)
    assert parsed.get_backend_name() == "postgresql", "CI concurrency coverage requires Postgres"
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(CreateSchema(schema))
    try:
        yield parsed.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    finally:
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def pilot(tmp_path, pilot_database_url):
    chain = FakeChain()
    settings = Settings(
        database_url=pilot_database_url,
        local_sqlite=True,
        domain="pilot.test",
        program_id=str(Keypair().pubkey()),
        skr_mint=str(Keypair().pubkey()),
        verifier_key_file=tmp_path / "unused",
    )
    from seeker.db import connect
    from seeker.migrate import apply_migrations
    engine = connect(settings.database_url)
    apply_migrations(engine)
    engine.dispose()
    app = create_app(settings, chain=chain)
    from seeker.worker import Worker
    app.state.worker = Worker(app.state.database, chain, settings, batch_size=100)
    c = TestClient(app)
    keys = [Keypair() for _ in range(4)]
    h = []
    for k in keys:
        r = c.post("/auth/challenge", json={"wallet": str(k.pubkey())}).json()
        v = c.post(
            "/auth/verify",
            json={
                "challenge_id": r["challenge_id"],
                "signature": str(k.sign_message(r["message"].encode())),
            },
        )
        assert v.status_code == 200, v.text
        h.append({"Authorization": "Bearer " + v.json()["token"]})
    yield c, app, h, keys, chain, settings
    app.state.database.engine.dispose()


def run_work(app):
    """Drive a separate worker after HTTP returns; skip wall-clock backoff in tests.

    Failures get one attempt per call. Dependency waits can advance within the
    call so reservation -> settlement -> anchor is deterministic.
    """
    from sqlalchemy import update
    with app.state.database.transaction() as db:
        db.execute(update(Job).where(Job.status == "pending").values(next_attempt_at=0))
    app.state.worker.reconcile()
    for _ in range(4):
        with app.state.database.transaction() as db:
            db.execute(update(Job).where(Job.status == "pending", Job.last_error.is_(None)).values(next_attempt_at=0))
        for job in sorted(app.state.worker.claim(), key=lambda j: j.type != "reservation"):
            app.state.worker.execute(job)


def submit(c, app, path, **kwargs):
    response = c.post(path, **kwargs)
    if response.status_code == 202:
        run_work(app)
    return response


def progress(app, response):
    from seeker.outbox import judgment_view
    with app.state.database.transaction() as db:
        return judgment_view(db, db.get(Judgment, response.json()["judgment_id"]))


def seed_calibration(app, n, known="a"):
    with app.state.database.transaction() as db:
        ids = []
        for _ in range(n):
            p = CalibrationPair(
                pair_id=__import__("uuid").uuid4().hex,
                known_choice=known,
                known_consensus=0.9,
                media_type="image",
                task_type="preference",
                output_a_url="https://x/a.png",
                output_b_url="https://x/b.png",
            )
            db.add(p)
            db.flush()
            ids.append(p.pair_id)
    return ids


def seed_pair(app, task_type="preference", k=3, batch_id=None):
    import uuid

    with app.state.database.transaction() as db:
        if batch_id is None:
            batch_id = uuid.uuid4().hex
            db.add(JudgmentBatch(batch_id=batch_id))
        p = JudgmentPair(
            pair_id=uuid.uuid4().hex,
            batch_id=batch_id,
            media_type="image",
            task_type=task_type,
            output_a_url="https://x/a.png",
            output_b_url=None if task_type == "rating" else "https://x/b.png",
            k=k,
        )
        db.add(p)
        db.flush()
        return p.pair_id


def calibrate(c, h, pair_ids, correct):
    for i, pid in enumerate(pair_ids):
        choice = "a" if i < correct else "b"
        r = c.post(f"/judge/{pid}", headers=h, json={"choice": choice})
        assert r.status_code == 200, r.text


def bond(c, h):
    assert c.post("/bond/faucet", headers=h).status_code == 202
    run_work(c.app)
    tx = c.post("/bond/deposit", headers=h).json()
    sig = str(Keypair().sign_message(tx["id"].encode()))
    r = c.post(f"/transactions/{tx['id']}/confirm", headers=h, json={"signature": sig})
    assert r.status_code == 200, r.text


def ready_judge(c, app, h, calib_ids):
    calibrate(c, h, calib_ids, correct=10)
    bond(c, h)
    wallet = c.get("/me", headers=h).json()["wallet"]
    session = c.post("/liveness/session", headers=h, json={"contributor": wallet, "task_id": "capture-session"}).json()
    response = c.post("/liveness/complete", headers=h, json={
        "contributor": wallet, "session_id": session["session_id"], "result_digest": "ab" * 32,
    })
    assert response.status_code == 200, response.text


# ---- calibration gate ----


def test_skr_wire_contract_matches_storage(pilot):
    c, app, headers, keys, _chain, settings = pilot
    health = c.get("/health").json()
    assert health["skr_mint"] == settings.skr_mint
    assert "skr_payout" not in health  # API cannot attest to worker signer readiness
    assert health["test_tokens"] is True
    assert not any("usdc" in key for key in health)

    bond(c, headers[0])
    me = c.get("/me", headers=headers[0]).json()
    stats = c.get("/judge/stats", headers=headers[0]).json()
    assert me["bond_test_skr_units"] == settings.min_bond
    assert me["skr_funded"] is True
    assert me["staked_skr"] == stats["staked_skr"] == settings.min_bond
    assert stats["base_skr"] == 20_000
    assert stats["flat_skr"] == 5_000
    assert stats["earned_skr"] == stats["settled_skr"] == 0
    assert not any("usdc" in key for key in me | stats)
    with app.state.database.transaction() as db:
        actor = db.get(Actor, str(keys[0].pubkey()))
        assert actor.skr_funded is True
        assert actor.staked_skr == settings.min_bond


def test_deposit_does_not_require_calibration(pilot):
    c, _app, h, _keys, _chain, _s = pilot
    assert c.post("/bond/faucet", headers=h[0]).status_code == 202
    run_work(c.app)
    r = c.post("/bond/deposit", headers=h[0])
    assert r.status_code == 200, r.text


def test_calibration_pass_at_eight_of_ten(pilot):
    c, app, h, _k, _ch, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    calibrate(c, h[0], ids, correct=8)
    stats = c.get("/judge/stats", headers=h[0]).json()
    assert stats["calibrated"] is True
    assert stats["calibration_progress"]["correct"] == 8


def test_calibration_fail_resets_progress(pilot):
    c, app, h, _k, _ch, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    calibrate(c, h[0], ids, correct=3)
    stats = c.get("/judge/stats", headers=h[0]).json()
    assert stats["calibrated"] is False
    assert stats["calibration_progress"]["completed"] == 0


# ---- consensus scoring ----


def test_consensus_majority_pays_aligned(pilot):
    c, app, h, keys, chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app)
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).json()["status"] == "accepted"
    assert submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "a"}).json()["status"] == "accepted"
    r = submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "b"})
    assert r.status_code == 202, r.text
    body = progress(app, r)
    assert body["pair_status"] == "resolved"
    assert body["preference"] == "a"
    assert abs(body["agreement"] - 2 / 3) < 1e-6

    w0, w1, w2 = [str(k.pubkey()) for k in keys[:3]]
    base = settings.judge_base_skr
    assert sorted(chain.settled_skr) == sorted([(w0, base), (w1, base)])
    assert all(w != w2 for w, _ in chain.settled_skr)


def test_cant_tell_reroutes_and_pays_flat(pilot):
    c, app, h, keys, chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app)
    submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "b"})
    r = submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "cant_tell"})
    assert r.status_code == 202, r.text
    assert progress(app, r)["pair_status"] == "collecting"
    # The "can't tell" judge earned the flat rate; nobody was resolved as aligned.
    w2 = str(keys[2].pubkey())
    assert (w2, settings.judge_flat_skr) in chain.settled_skr


def test_tie_reroutes(pilot):
    c, app, h, _keys, _chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app)
    submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "b"})
    r = submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "tie"})
    assert progress(app, r)["pair_status"] == "collecting"


def test_rating_median_alignment(pilot):
    c, app, h, keys, chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app, task_type="rating")
    submit(c, app, f"/judge/{pid}", headers=h[0], json={"rating": 4})
    submit(c, app, f"/judge/{pid}", headers=h[1], json={"rating": 4})
    r = submit(c, app, f"/judge/{pid}", headers=h[2], json={"rating": 2})
    assert progress(app, r)["pair_status"] == "resolved"
    w0, w1, w2 = [str(k.pubkey()) for k in keys[:3]]
    assert sorted(chain.settled_skr) == sorted([(w0, settings.judge_base_skr), (w1, settings.judge_base_skr)])


# ---- agreement-floor slash trigger ----


def test_withdraw_frozen_after_warn(pilot):
    # Finding A (blockchain-dev): the on-chain program allows withdraw after the
    # first warn, letting a worker drain the vault before the second-strike
    # slash. The backend must refuse /bond/withdraw once warnings >= 1.
    c, app, h, keys, chain, settings = pilot
    w0 = str(keys[0].pubkey())
    with app.state.database.transaction() as db:
        a = db.get(Actor, w0)
        a.bonded = True
        a.warnings = 1
    r = c.post("/bond/withdraw", headers=h[0])
    assert r.status_code == 403
    assert "warning" in r.json()["detail"].lower()
    # clean worker can still withdraw
    with app.state.database.transaction() as db:
        db.get(Actor, w0).warnings = 0
    r = c.post("/bond/withdraw", headers=h[0])
    assert r.status_code == 200


def test_dissenter_below_floor_is_warned(pilot):
    c, app, h, keys, chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    # Simulate a judge with a long dissenting history so a fresh dissent pushes
    # them below the slash floor (default min 20 judged).
    w2 = str(keys[2].pubkey())
    with app.state.database.transaction() as db:
        db.get(Actor, w2).judged_count = 30
        db.get(Actor, w2).agreement_rate = 0.0
    pid = seed_pair(app)
    submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "b"})
    # Judge 2 dissented with a rate now below the floor -> warn issued.
    assert ("warn", w2, None) in chain.issued
    with app.state.database.transaction() as db:
        assert db.get(Actor, w2).warnings == 1


# ---- pure helpers ----


def test_judge_ladder_thresholds():
    assert level_for(0.0) == 0
    assert level_for(0.80) == 1
    assert level_for(0.88) == 2
    assert level_for(0.93) == 3
    assert level_for(0.97) == 4
    assert rate_multiplier(4) == 2.00


def test_merkle_root_deterministic():
    entries = [("p1", "a", 0.5, 0.5), ("p2", "b", 1.0, 1.0)]
    r1 = judgment_mod.merkle_root(entries)
    r2 = judgment_mod.merkle_root(entries)
    assert r1 == r2 and r1 is not None
    assert judgment_mod.merkle_root([]) is None


# ---- adversarial regressions (Astra finding 10) ----


def test_tie_vote_is_neutral_not_dissent(pilot):
    """A ``tie`` vote must not be scored as dissent when a/b has a clear winner
    (finding #5). a,a,tie must re-route (tie is consumed, pair keeps collecting)
    and the tie judge's agreement rate must be untouched."""
    c, app, h, keys, _chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app)
    submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    r = submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "tie"})
    assert progress(app, r)["pair_status"] == "collecting"
    # The tie judge did not dissent: their agreement rate is still neutral
    # (default 0.0, not lowered), and their judged_count did not advance.
    w2 = str(keys[2].pubkey())
    with app.state.database.transaction() as db:
        a = db.get(Actor, w2)
        assert a.judged_count == 0
        assert a.agreement_rate == 0.0
    # No judge was paid for the unresolved pair.
    assert _chain.settled_skr == []


def test_calibration_correctness_hidden_mid_gate(pilot):
    """Mid-gate, per-answer correctness must NOT be exposed (finding #6): a judge
    who sees a running correct count can binary-search the known answer."""
    c, app, h, _k, _ch, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    # One answer in, still in progress: `correct` must be None.
    r = c.post(f"/judge/{ids[0]}", headers=h[0], json={"choice": "a"})
    assert r.status_code == 200
    assert r.json()["calibration_progress"]["correct"] is None
    stats = c.get("/judge/stats", headers=h[0]).json()
    assert stats["calibration_progress"]["correct"] is None


def test_payout_obligation_persisted_before_broadcast(pilot):
    """A failed settle must leave the earned_skr obligation journaled (settling
    flag) so a retry reconciles it, not re-creates it (finding #1)."""
    c, app, h, keys, chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app)

    calls = {"n": 0}

    def flaky_settle(wallet, amount, memo=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChainUnavailable("boom")
        return str(Keypair().sign_message(b"ok"))

    chain.settle_skr = flaky_settle
    submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    r = submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "b"})
    # HTTP accepts durably; failed worker payout leaves the obligation journaled.
    assert r.status_code == 202
    with app.state.database.transaction() as db:
        p = db.get(JudgmentPair, pid)
        assert p.status == "resolving"
        settling = [
            j for j in db.query(Judgment).filter(Judgment.pair_id == pid).all()
            if j.settling
        ]
        assert len(settling) >= 1
        assert settling[0].earned_skr == settings.judge_base_skr


def test_direct_submission_requires_liveness_even_with_bond(pilot):
    c, app, h, keys, chain, settings = pilot
    bond(c, h[0])
    pid = seed_pair(app, k=1)
    response = submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    assert response.status_code == 403
    assert "human" in response.json()["detail"]
    with app.state.database.transaction() as db:
        assert db.query(Judgment).count() == 0
    assert not chain.reservations and not chain.settled_skr
    settings.liveness_required = False
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).status_code == 202


@pytest.mark.parametrize("result_digest", [
    "deadbeef" * 8,  # Exact Continue (demo) payload in LivenessScreen.kt.
    hashlib.sha256(b"v2|BLINK|PASSED|10|10|0|10|0|live").hexdigest(),
])
def test_liveness_completion_persists_and_unblocks_judging(pilot, result_digest):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_demo = result_digest == "deadbeef" * 8
    settings.liveness_required = True
    wallet = str(keys[0].pubkey())
    bond(c, h[0])
    pid = seed_pair(app)
    assert c.get("/me", headers=h[0]).json()["liveness_completed"] is False
    assert c.get("/judge/next", headers=h[0]).json()["kind"] == "none"
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).status_code == 403

    issued = c.post("/liveness/session", headers=h[0], json={
        "contributor": wallet, "task_id": "capture-session",
    })
    assert issued.status_code == 200, issued.text
    session = issued.json()
    assert session["challenge"] in {"blink", "turn_left", "turn_right", "smile"}
    body = {
        "contributor": wallet, "session_id": session["session_id"],
        "result_digest": result_digest,
    }
    completed = c.post("/liveness/complete", headers=h[0], json=body)
    assert completed.status_code == 200, completed.text
    assert len(bytes.fromhex(completed.json()["liveness_proof_hash"])) == 32
    with app.state.database.transaction() as db:
        actor = db.get(Actor, wallet)
        assert actor.liveness_method == ("demo" if settings.liveness_demo else "motion")
        assert actor.liveness_verified_at < actor.liveness_expires_at
        assert actor.liveness_expires_at - actor.liveness_verified_at == settings.liveness_verification_seconds
        assert actor.liveness_device_integrity == "none"
        assert actor.liveness_completed is False  # Legacy flag is no longer evidence.
    assert c.get("/me", headers=h[0]).json()["liveness_completed"] is True
    assert c.get("/me", headers=h[1]).json()["liveness_completed"] is False
    assert c.get("/judge/next", headers=h[0]).json()["pair_id"] == pid
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).status_code == 202
    assert c.post("/liveness/complete", headers=h[0], json=body).status_code == 409


@pytest.mark.parametrize("result_digest", ["00" * 32, "not-hex"])
def test_invalid_liveness_result_keeps_gate_closed_and_allows_retry(pilot, result_digest):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = True
    wallet = str(keys[0].pubkey())
    session = c.post("/liveness/session", headers=h[0], json={
        "contributor": wallet, "task_id": "capture-session",
    }).json()
    body = {
        "contributor": wallet, "session_id": session["session_id"],
        "result_digest": result_digest,
    }
    assert c.post("/liveness/complete", headers=h[0], json=body).status_code == 400
    assert c.get("/me", headers=h[0]).json()["liveness_completed"] is False
    body["result_digest"] = "12" * 32
    assert c.post("/liveness/complete", headers=h[0], json=body).status_code == 200


def test_expired_liveness_session_can_be_replaced(pilot):
    from dataclasses import replace
    from seeker.api import liveness_sessions

    c, app, h, keys, chain, settings = pilot
    wallet = str(keys[0].pubkey())
    request = {"contributor": wallet, "task_id": "capture-session"}
    session = c.post("/liveness/session", headers=h[0], json=request).json()
    store = liveness_sessions()
    sid = session["session_id"]
    store._sessions[sid] = replace(store.get(wallet, sid), expires_at=0)
    body = {"contributor": wallet, "session_id": sid, "result_digest": "12" * 32}
    assert c.post("/liveness/complete", headers=h[0], json=body).status_code == 410
    assert c.get("/me", headers=h[0]).json()["liveness_completed"] is False
    replacement = c.post("/liveness/session", headers=h[0], json=request).json()
    body["session_id"] = replacement["session_id"]
    assert c.post("/liveness/complete", headers=h[0], json=body).status_code == 200
    assert c.get("/me", headers=h[0]).json()["liveness_completed"] is True


def test_retry_after_mint_confirmation_loss_does_not_deadlock_or_double_pay(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    bond(c, h[0])
    pid = seed_pair(app, k=1)
    original = chain.settle_skr

    def lost_confirmation(wallet, amount, memo, **kwargs):
        signature = original(wallet, amount, memo, **kwargs)
        with app.state.database.transaction() as db:
            j = db.get(Judgment, memo)
            assert j.payout_pending_signature == signature
            assert j.payout_transaction == "SIGNED-TEST-TX"
            assert j.settling and j.payout_signature is None
        raise ChainUnavailable("confirmation lost after successful mint")

    chain.settle_skr = lost_confirmation
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).status_code == 202
    assert len(chain.settled_skr) == 1
    chain.settle_skr = original
    # SQLite BEGIN IMMEDIATE would self-deadlock here with the old retry path.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(c.post, f"/judge/{pid}", headers=h[0], json={"choice": "a"}) for _ in range(2)]
        assert all(f.result(timeout=5).status_code == 202 for f in results)
    run_work(app)
    assert len(chain.settled_skr) == 1
    with app.state.database.transaction() as db:
        assert db.get(JudgmentPair, pid).status == "resolved"
        assert db.get(Actor, str(keys[0].pubkey())).judged_count == 1
        j = db.query(Judgment).one()
        assert not j.settling and j.enforcement_complete
    assert not chain.reservations


def test_enforcement_failure_is_durable_and_concurrent_retries_warn_once(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    settings.judge_slash_min_judged = 1
    for hd in h[:3]:
        bond(c, hd)
    pid = seed_pair(app)
    original = chain.resolve_judgment

    def lose_confirmation(wallet, rid, penalize):
        result = original(wallet, rid, penalize)
        if penalize:
            raise ChainUnavailable("warning landed but confirmation lost")
        return result

    chain.resolve_judgment = lose_confirmation
    for i, choice in enumerate(("a", "a", "b")):
        r = submit(c, app, f"/judge/{pid}", headers=h[i], json={"choice": choice})
    assert r.status_code == 202
    with app.state.database.transaction() as db:
        assert db.get(JudgmentPair, pid).status == "enforcing"
        penalty = db.query(Judgment).filter_by(enforcement_penalty=True).one()
        assert penalty.enforcement_requested and not penalty.enforcement_complete
    chain.resolve_judgment = original
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = [pool.submit(c.post, f"/judge/{pid}", headers=h[2], json={"choice": "b"}) for _ in range(2)]
        assert all(f.result(timeout=5).status_code == 202 for f in replies)
    run_work(app)
    wallet = str(keys[2].pubkey())
    assert chain.issued.count(("warn", wallet, None)) == 1
    with app.state.database.transaction() as db:
        actor = db.get(Actor, wallet)
        assert actor.warnings == 1 and actor.judged_count == 1
        assert db.get(JudgmentPair, pid).status == "resolved"


def test_reservation_unknown_result_retries_same_durable_id(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    bond(c, h[0])
    pid = seed_pair(app)
    original = chain.reserve_judgment

    def lost_confirmation(wallet, rid):
        original(wallet, rid)
        raise ChainUnavailable("reserve landed but response lost")

    chain.reserve_judgment = lost_confirmation
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).status_code == 202
    with app.state.database.transaction() as db:
        row = db.query(Judgment).one()
        rid = row.judgment_id
        assert not row.liability_reserved
        assert db.get(JudgmentPair, pid).status == "open"
    chain.reserve_judgment = original
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "a"}).status_code == 202
    assert chain.reservations == {(str(keys[0].pubkey()), rid)}


def test_neutral_payout_and_release_retry(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    bond(c, h[0])
    pid = seed_pair(app)
    original = chain.resolve_judgment
    chain.resolve_judgment = lambda *args: (_ for _ in ()).throw(ChainUnavailable("offline"))
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "cant_tell"}).status_code == 202
    chain.resolve_judgment = original
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "cant_tell"}).status_code == 202
    assert len(chain.settled_skr) == 1 and not chain.reservations


def test_migration_preserves_legacy_votes_and_reserves_before_consensus(pilot):
    from seeker.migrate import apply_migrations
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    for hd in h[:2]:
        bond(c, hd)
    pid = seed_pair(app, k=2)
    with app.state.database.transaction() as db:
        db.add(Judgment(pair_id=pid, wallet=str(keys[0].pubkey()), choice="a"))
        db.get(JudgmentPair, pid).status = "collecting"
    # Emulate an accepted pre-upgrade judgment, then apply the migration twice.
    with app.state.database.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE judgments DROP COLUMN liability_legacy")
        conn.exec_driver_sql("DELETE FROM schema_versions WHERE version >= '0015'")
    assert apply_migrations(app.state.database.engine) == 4
    assert apply_migrations(app.state.database.engine) == 0
    with app.state.database.transaction() as db:
        assert db.query(Judgment).one().liability_legacy
    response = submit(c, app, f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    assert response.status_code == 202, response.text
    assert progress(app, response)["pair_status"] == "resolved"
    assert len(chain.settled_skr) == 2
    with app.state.database.transaction() as db:
        assert all(j.liability_reserved and not j.liability_legacy for j in db.query(Judgment).all())
        assert db.get(Actor, str(keys[0].pubkey())).judged_count == 1


def test_abandoned_unconfirmed_submission_is_cancelled_at_resolution(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    for hd in h[:3]:
        bond(c, hd)
    pid = seed_pair(app, k=2)
    original = chain.reserve_judgment

    def lost_confirmation(wallet, rid):
        original(wallet, rid)
        if wallet == str(keys[0].pubkey()):
            raise ChainUnavailable("reservation may have landed")

    chain.reserve_judgment = lost_confirmation
    assert submit(c, app, f"/judge/{pid}", headers=h[0], json={"choice": "b"}).status_code == 202
    for hd in h[1:3]:
        response = submit(c, app, f"/judge/{pid}", headers=hd, json={"choice": "a"})
    assert progress(app, response)["pair_status"] == "resolved"
    assert not chain.reservations
    with app.state.database.transaction() as db:
        j = db.query(Judgment).filter_by(wallet=str(keys[0].pubkey())).one()
        assert j.consumed and j.enforcement_complete and not j.enforcement_penalty
        assert db.get(Actor, j.wallet).judged_count == 0


def test_second_resolution_slashes_once_and_slashed_judge_can_reconcile(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    settings.judge_slash_min_judged = 1
    for hd in h[:3]:
        bond(c, hd)
    for _ in range(2):
        pid = seed_pair(app)
        for i, choice in enumerate(("a", "a", "b")):
            response = submit(c, app, f"/judge/{pid}", headers=h[i], json={"choice": choice})
        assert response.status_code == 202, response.text
    wallet = str(keys[2].pubkey())
    with app.state.database.transaction() as db:
        actor = db.get(Actor, wallet)
        assert actor.warnings == 2 and not actor.bonded and actor.staked_skr == 0
        assert actor.judged_count == 2
    assert submit(c, app, f"/judge/{pid}", headers=h[2], json={"choice": "b"}).status_code == 202
    assert chain.issued.count(("warn", wallet, None)) == 2
    assert chain.issued.count(("slash", wallet, None)) == 1


@pytest.mark.parametrize("mode", ["confirm", "reconcile", "worker"])
@pytest.mark.parametrize("on_chain_warnings", [0, 1, 2, 255])
def test_deposit_reconciles_warning_mirror(pilot, mode, on_chain_warnings):
    c, app, h, keys, chain, settings = pilot
    wallet = str(keys[0].pubkey())
    with app.state.database.transaction() as db:
        actor = db.get(Actor, wallet)
        actor.skr_funded = True
        actor.warnings = 2
    prepared = c.post("/bond/deposit", headers=h[0]).json()
    chain.worker_warnings[wallet] = on_chain_warnings
    path = f"/transactions/{prepared['id']}/{mode}"
    payload = {"signature": str(keys[0].sign_message(b"deposit"))} if mode == "confirm" else {}
    if mode == "worker":
        run_work(app)
    else:
        response = c.post(path, headers=h[0], json=payload)
        assert response.status_code == 200, response.text
    with app.state.database.transaction() as db:
        actor = db.get(Actor, wallet)
        assert actor.bonded and actor.staked_skr == settings.min_bond
        assert actor.warnings == min(on_chain_warnings, 2)
    for route in ("/bond/withdraw", "/bond/withdraw/prepare"):
        assert c.post(route, headers=h[0]).status_code == (200 if on_chain_warnings == 0 else 403)
    # Replaying an already-confirmed deposit must not clear a newer warning.
    with app.state.database.transaction() as db:
        db.get(Actor, wallet).warnings = 1
    chain.worker_warnings[wallet] = 0
    assert c.post(f"/transactions/{prepared['id']}/reconcile", headers=h[0]).status_code == 200
    assert c.get("/me", headers=h[0]).json()["warnings"] == 1


@pytest.mark.parametrize("mode", ["confirm", "reconcile", "worker"])
def test_deposit_warning_read_failure_does_not_confirm_or_clear_mirror(pilot, mode):
    from seeker.db import Prepared
    c, app, h, keys, chain, settings = pilot
    wallet = str(keys[0].pubkey())
    with app.state.database.transaction() as db:
        actor = db.get(Actor, wallet)
        actor.skr_funded, actor.warnings = True, 2
    prepared = c.post("/bond/deposit", headers=h[0]).json()
    original = chain.bond_warnings
    def unavailable(wallet):
        raise ChainUnavailable("Bond state unavailable")
    chain.bond_warnings = unavailable
    if mode == "worker":
        run_work(app)
    else:
        payload = {"signature": str(keys[0].sign_message(b"deposit"))} if mode == "confirm" else {}
        assert c.post(f"/transactions/{prepared['id']}/{mode}", headers=h[0], json=payload).status_code == 503
    with app.state.database.transaction() as db:
        assert not db.get(Prepared, prepared["id"]).confirmed
        actor = db.get(Actor, wallet)
        assert not actor.bonded and actor.warnings == 2
    chain.bond_warnings = original
    assert c.post(f"/transactions/{prepared['id']}/reconcile", headers=h[0]).status_code == 200
    assert c.get("/me", headers=h[0]).json()["warnings"] == 0


def test_unavailable_pair_exclusions_are_unpaid_and_request_scoped(pilot):
    from sqlalchemy import select
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    with app.state.database.transaction() as db:
        for key in keys[:2]:
            db.get(Actor, str(key.pubkey())).bonded = True
    ids = [seed_pair(app) for _ in range(3)]
    excluded = []
    for _ in ids:
        response = c.get("/judge/next", headers=h[0], params=excluded)
        assert response.status_code == 200, response.text
        pair = response.json()["pair_id"]
        assert pair not in [value for _, value in excluded]
        excluded.append(("exclude_pair_id", pair))
    assert c.get("/judge/next", headers=h[0], params=excluded).json()["kind"] == "none"
    assert c.get("/judge/next", params=excluded).status_code == 401
    # Exclusion cannot mutate a pair globally or manufacture a paid abstention.
    assert c.get("/judge/next", headers=h[1]).json()["pair_id"] in ids
    assert c.get("/judge/next", headers=h[0]).json()["pair_id"] in ids
    with app.state.database.transaction() as db:
        assert not list(db.scalars(select(Judgment)))
        assert not list(db.scalars(select(Job)))
        assert all(p.status == "open" for p in db.scalars(select(JudgmentPair)))
        assert db.get(Actor, str(keys[0].pubkey())).judged_count == 0
    assert not chain.reservations and not chain.settled_skr
    # Submitted pairs stay excluded even when absent from the skip list.
    assert c.post(f"/judge/{ids[0]}", headers=h[0], json={"choice": "a"}).status_code == 202
    assert c.get("/judge/next", headers=h[0]).json()["pair_id"] != ids[0]
