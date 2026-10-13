"""Judgment product (RLHF preference judging) — consensus, calibration, ladder,
$SKR payout, reroute, and the agreement-floor slash trigger."""

import base64

import pytest
from fastapi.testclient import TestClient
from seeker.api import create_app
from seeker.config import Settings
from seeker.db import Actor, Base, CalibrationPair, Judgment, JudgmentBatch, JudgmentPair
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
        return self.warnings

    def bond_vault_balance(self, wallet):
        return 1_000_000  # >= min_bond; test judges are assumed bonded

    def issue(self, action, wallet, amount=None):
        self.issued.append((action, wallet, amount))
        return str(Keypair().sign_message(f"issue-{len(self.issued)}".encode()))

    def settle_skr(self, wallet, amount):
        return str(Keypair().sign_message(b"skr"))

    def settle_skr(self, wallet, amount, memo=None):
        self.settled_skr.append((wallet, amount))
        return str(Keypair().sign_message(f"skr-{len(self.settled_skr)}".encode()))

    def faucet(self, wallet):
        return str(Keypair().sign_message(b"faucet"))

    def post_batch(self, batch_id, merkle_root):
        self.posted.append((batch_id, merkle_root))
        return str(Keypair().sign_message(b"batch"))


@pytest.fixture
def pilot(tmp_path):
    chain = FakeChain()
    settings = Settings(
        database_url="sqlite:///" + str(tmp_path / "judge.db"),
        local_sqlite=True,
        domain="pilot.test",
        program_id=str(Keypair().pubkey()),
        skr_mint=str(Keypair().pubkey()),
        verifier_key_file=tmp_path / "unused",
    )
    app = create_app(settings, chain=chain)
    Base.metadata.drop_all(app.state.database.engine)
    Base.metadata.create_all(app.state.database.engine)
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
    assert c.post("/bond/faucet", headers=h).status_code == 200
    tx = c.post("/bond/deposit", headers=h).json()
    sig = str(Keypair().sign_message(tx["id"].encode()))
    r = c.post(f"/transactions/{tx['id']}/confirm", headers=h, json={"signature": sig})
    assert r.status_code == 200, r.text


def ready_judge(c, app, h, calib_ids):
    calibrate(c, h, calib_ids, correct=10)
    bond(c, h)


# ---- calibration gate ----


def test_skr_wire_contract_keeps_legacy_storage(pilot):
    c, app, headers, keys, _chain, settings = pilot
    health = c.get("/health").json()
    assert health["skr_mint"] == settings.skr_mint
    assert health["skr_payout"] == "unavailable"
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
        assert actor.usdc_funded is True
        assert actor.staked_usdc == settings.min_bond


def test_deposit_does_not_require_calibration(pilot):
    c, _app, h, _keys, _chain, _s = pilot
    assert c.post("/bond/faucet", headers=h[0]).status_code == 200
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
    assert c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"}).json()["status"] == "submitted"
    assert c.post(f"/judge/{pid}", headers=h[1], json={"choice": "a"}).json()["status"] == "submitted"
    r = c.post(f"/judge/{pid}", headers=h[2], json={"choice": "b"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
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
    c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    c.post(f"/judge/{pid}", headers=h[1], json={"choice": "b"})
    r = c.post(f"/judge/{pid}", headers=h[2], json={"choice": "cant_tell"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rerouted"
    # The "can't tell" judge earned the flat rate; nobody was resolved as aligned.
    w2 = str(keys[2].pubkey())
    assert (w2, settings.judge_flat_skr) in chain.settled_skr


def test_tie_reroutes(pilot):
    c, app, h, _keys, _chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app)
    c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    c.post(f"/judge/{pid}", headers=h[1], json={"choice": "b"})
    r = c.post(f"/judge/{pid}", headers=h[2], json={"choice": "tie"})
    assert r.json()["status"] == "rerouted"


def test_rating_median_alignment(pilot):
    c, app, h, keys, chain, settings = pilot
    ids = seed_calibration(app, settings.calibration_n)
    for hd in h[:3]:
        ready_judge(c, app, hd, ids)
    pid = seed_pair(app, task_type="rating")
    c.post(f"/judge/{pid}", headers=h[0], json={"rating": 4})
    c.post(f"/judge/{pid}", headers=h[1], json={"rating": 4})
    r = c.post(f"/judge/{pid}", headers=h[2], json={"rating": 2})
    assert r.json()["status"] == "resolved"
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
    c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    c.post(f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    c.post(f"/judge/{pid}", headers=h[2], json={"choice": "b"})
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
    c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    c.post(f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    r = c.post(f"/judge/{pid}", headers=h[2], json={"choice": "tie"})
    assert r.json()["status"] == "rerouted"
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

    def flaky_settle(wallet, amount, memo=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChainUnavailable("boom")
        return str(Keypair().sign_message(b"ok"))

    chain.settle_skr = flaky_settle
    c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    c.post(f"/judge/{pid}", headers=h[1], json={"choice": "a"})
    r = c.post(f"/judge/{pid}", headers=h[2], json={"choice": "b"})
    # First obligation fails -> 503, pair left resolving, obligation journaled.
    assert r.status_code == 503
    with app.state.database.transaction() as db:
        p = db.get(JudgmentPair, pid)
        assert p.status == "resolving"
        settling = [
            j for j in db.query(Judgment).filter(Judgment.pair_id == pid).all()
            if j.settling
        ]
        assert len(settling) >= 1
        assert settling[0].earned_usdc == settings.judge_base_skr
