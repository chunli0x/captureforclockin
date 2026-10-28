"""Keyless HTTP, worker signing and faucet crash recovery (no live funds)."""
import base64
import json
import struct
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from solders.hash import Hash
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import Transaction
from sqlalchemy import select

from seeker.api import create_app
from seeker.chain import Chain, ChainUnavailable, discriminator
from seeker.db import Actor, Job
from seeker.signing_chain import SigningChain
from seeker.worker import Worker, MAX_ATTEMPTS
from test_judgment import pilot_database_url, pilot, run_work, seed_pair


def signer_settings(settings, tmp_path):
    verifier, treasury = Keypair(), Keypair()
    for name, key in (("verifier", verifier), ("treasury", treasury)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(list(bytes(key))))
        setattr(settings, f"{name}_key_file", path)
        setattr(settings, f"{name}_pubkey", str(key.pubkey()))
    return verifier, treasury


@pytest.mark.parametrize("files", ["absent", "unset", "present"])
def test_api_health_and_user_prepare_never_open_key_files(pilot, tmp_path, monkeypatch, files):
    _, old_app, headers, keys, _, settings = pilot
    verifier, treasury = signer_settings(settings, tmp_path)
    for name in ("verifier", "treasury"):
        path = getattr(settings, f"{name}_key_file")
        if files != "present":
            path.unlink()
        if files == "unset":
            setattr(settings, f"{name}_key_file", None)
    original = Path.read_text
    def no_key_read(path, *args, **kwargs):
        assert path.name not in {"verifier.json", "treasury.json"}, "API opened a key file"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_key_read)

    def rpc(chain, method, params):
        if method == "getGenesisHash":
            from seeker.chain import DEVNET_GENESIS
            return DEVNET_GENESIS
        if method == "getAccountInfo":
            raw = (discriminator("Config", "account") + bytes(treasury.pubkey())
                   + bytes(verifier.pubkey()) + bytes(chain.skr)
                   + struct.pack("<Q", settings.min_bond) + bytes(verifier.pubkey()))
            return {"value": {"owner": str(chain.program), "data": [base64.b64encode(raw).decode(), "base64"]}}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique()), "lastValidBlockHeight": 100}}
        raise AssertionError(method)
    monkeypatch.setattr(Chain, "rpc", rpc)
    app = create_app(settings)  # The production factory path, not an injected FakeChain.
    try:
        with TestClient(app) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text
            assert "skr_payout" not in health.json()
            with old_app.state.database.transaction() as db:
                db.get(Actor, str(keys[0].pubkey())).skr_funded = True
            for action in ("deposit", "withdraw"):
                with old_app.state.database.transaction() as db:
                    db.get(Actor, str(keys[0].pubkey())).bonded = True
                result = client.post(f"/bond/{action}/prepare", headers=headers[0])
                assert result.status_code == 200, result.text
                tx = Transaction.from_bytes(base64.b64decode(result.json()["transaction_base64"]))
                assert tx.message.header.num_required_signatures == 1
                assert tx.message.account_keys[0] == keys[0].pubkey()
                assert tx.signatures == [Signature.default()]
                tx.sign([keys[0]], tx.message.recent_blockhash)
                tx.verify()  # The user's signature alone is sufficient.
            assert not hasattr(app.state.chain, "verifier")
            assert not hasattr(app.state.chain, "treasury")
            assert not hasattr(app.state.chain, "faucet")
    finally:
        app.state.database.engine.dispose()


def test_faucet_acceptance_is_atomic_scoped_and_worker_only(pilot, monkeypatch):
    c, app, h, keys, chain, settings = pilot
    calls = []
    def faucet(wallet, *, persist, prepared=None):
        calls.append(wallet)
        return "confirmed"
    monkeypatch.setattr(chain, "faucet", faucet)
    response = c.post("/bond/faucet", headers=h[0])
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted" and not body["funded"]
    assert calls == []
    assert c.post("/bond/faucet", headers=h[0]).json()["operation_id"] == body["operation_id"]
    path = f'/operations/{body["operation_id"]}'
    assert c.get(path, headers=h[1]).status_code == 404
    assert c.get(path).status_code == 401
    run_work(app)
    done = c.get(path, headers=h[0]).json()
    assert done["status"] == "finalized" and done["funded"]
    assert done["mints"] == 1 and calls == [str(keys[0].pubkey())]
    assert "prepared" not in done and "transaction" not in done

    from seeker import api
    original = api.enqueue
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("pre-commit crash")
    monkeypatch.setattr(api, "enqueue", crash)
    with pytest.raises(RuntimeError):
        c.post("/bond/faucet", headers=h[0])
    with app.state.database.transaction() as db:
        assert db.get(Actor, str(keys[0].pubkey())).faucet_count == 1
        assert len(list(db.scalars(select(Job).where(Job.type == "faucet")))) == 1


def install_rpc(chain, app, *, fail_send=False):
    sent = []
    def rpc(method, params):
        if method == "getBalance":
            return {"value": 10_000_000_000}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique()), "lastValidBlockHeight": 100}}
        if method == "getAccountInfo":
            return {"value": None}
        if method == "getBlockHeight":
            return 10
        if method == "sendTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            tx.verify()  # Real Ed25519 signatures, no fake signing client.
            assert tx.message.account_keys[0] == chain.treasury.pubkey()
            with app.state.database.transaction() as db:
                jobs = list(db.scalars(select(Job).where(Job.type == "faucet")))
                assert any(j.payload.get("prepared", {}).get("transaction") == params[0] for j in jobs)
            sent.append(params[0])
            if fail_send:
                raise ChainUnavailable("ambiguous broadcast")
            return str(tx.signatures[0])
        if method == "getSignatureStatuses":
            return {"value": [{"err": None, "confirmationStatus": "finalized"}]}
        raise AssertionError(method)
    chain.rpc = rpc
    return sent


def test_worker_cli_starts_with_keys_and_signs_faucet(pilot, tmp_path, monkeypatch):
    c, app, h, keys, _, settings = pilot
    signer_settings(settings, tmp_path)
    chain = SigningChain(settings)
    sent = install_rpc(chain, app)
    op = c.post("/bond/faucet", headers=h[0]).json()["operation_id"]
    # Run the real worker entry point once; no existing service is touched.
    from seeker import config, worker
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(SigningChain, "rpc", lambda self, method, params: chain.rpc(method, params))
    monkeypatch.setattr(sys, "argv", ["seeker.worker", "--once"])
    worker.main()
    assert len(sent) == 1
    assert c.get(f"/operations/{op}", headers=h[0]).json()["status"] == "finalized"


@pytest.mark.parametrize("window", ["persisted", "broadcast", "funded"])
def test_faucet_crash_recovery_keeps_quota_and_signature(pilot, tmp_path, monkeypatch, window):
    c, app, h, keys, _, settings = pilot
    signer_settings(settings, tmp_path)
    chain = SigningChain(settings)
    app.state.worker = Worker(app.state.database, chain, settings)
    sent = install_rpc(chain, app, fail_send=window == "broadcast")
    op = c.post("/bond/faucet", headers=h[0]).json()["operation_id"]
    if window == "persisted":
        original = chain.faucet
        def crash(wallet, *, persist, prepared=None):
            def saved(data):
                persist(data)
                raise ChainUnavailable("crash before broadcast")
            return original(wallet, persist=saved, prepared=prepared)
        monkeypatch.setattr(chain, "faucet", crash)
    if window == "funded":
        monkeypatch.setattr(app.state.worker, "finish", lambda *args: False)
    run_work(app)
    with app.state.database.transaction() as db:
        job = db.get(Job, op)
        journal = job.payload["prepared"]
        assert db.get(Actor, str(keys[0].pubkey())).faucet_count == 1
        if window != "funded":
            assert not db.get(Actor, str(keys[0].pubkey())).skr_funded
        job.lease_until = 0
        job.next_attempt_at = 0
    # Fresh process state. Recover using only the durable journal.
    fresh = SigningChain(settings)
    replay = install_rpc(fresh, app)
    if window == "persisted":
        original_rpc = fresh.rpc
        polls = []
        def pending_rpc(method, params):
            if method == "getSignatureStatuses" and not polls:
                polls.append(True)
                return {"value": [None]}
            return original_rpc(method, params)
        fresh.rpc = pending_rpc
    app.state.worker = Worker(app.state.database, fresh, settings)
    run_work(app)
    result = c.get(f"/operations/{op}", headers=h[0]).json()
    assert result["status"] == "finalized" and result["funded"]
    assert result["signature"] == journal["signature"] and result["mints"] == 1
    assert sent + replay == [journal["transaction"]]


def test_expired_ambiguous_faucet_never_resigns_or_refunds_quota(pilot, tmp_path):
    c, app, h, keys, _, settings = pilot
    settings.faucet_max_mints = 1
    signer_settings(settings, tmp_path)
    chain = SigningChain(settings)
    sent = install_rpc(chain, app, fail_send=True)
    app.state.worker = Worker(app.state.database, chain, settings)
    op = c.post("/bond/faucet", headers=h[0]).json()["operation_id"]
    run_work(app)
    def expired(method, params):
        if method == "getSignatureStatuses":
            return {"value": [None]}
        if method == "getBlockHeight":
            return 101
        raise AssertionError("Expired faucet must never build or broadcast another transaction")
    chain.rpc = expired
    for _ in range(MAX_ATTEMPTS):
        run_work(app)
    result = c.get(f"/operations/{op}", headers=h[0]).json()
    assert result["status"] == "failed" and not result["funded"]
    assert result["mints"] == 1 and len(sent) == 1
    assert c.post("/bond/faucet", headers=h[0]).json()["already"]


def test_worker_rejects_missing_or_mismatched_keys(pilot, tmp_path):
    *_, settings = pilot
    with pytest.raises(ChainUnavailable, match="verifier key"):
        SigningChain(settings)
    signer_settings(settings, tmp_path)
    settings.verifier_pubkey = str(Keypair().pubkey())
    with pytest.raises(ChainUnavailable, match="does not match"):
        SigningChain(settings)


def test_worker_settlement_signs_with_loaded_treasury(pilot, tmp_path, monkeypatch):
    c, app, h, keys, fake, settings = pilot
    signer_settings(settings, tmp_path)
    signer = SigningChain(settings)
    sent = []
    def rpc(method, params):
        if method == "getAccountInfo":
            return {"value": None}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique()), "lastValidBlockHeight": 100}}
        if method == "sendTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            tx.verify()
            assert tx.message.account_keys[0] == signer.treasury.pubkey()
            sent.append(tx)
            return str(tx.signatures[0])
        if method == "getSignatureStatuses":
            return {"value": [{"err": None, "confirmationStatus": "finalized"}]}
        raise AssertionError(method)
    signer.rpc = rpc
    # Other lifecycle effects use the existing fixture; the payout uses loaded
    # key files, the real mint instructions and real cryptographic signing.
    monkeypatch.setattr(fake, "settle_skr", signer.settle_skr)
    settings.liveness_required = False
    with app.state.database.transaction() as db:
        db.get(Actor, str(keys[0].pubkey())).bonded = True
    pid = seed_pair(app, k=1)
    accepted = c.post(f"/judge/{pid}", headers=h[0], json={"choice": "a"})
    assert accepted.status_code == 202
    run_work(app)
    assert len(sent) == 1
    result = c.get(f'/operations/{accepted.json()["operation_id"]}', headers=h[0]).json()
    assert result["payout"]["status"] == "finalized"


def test_stale_faucet_worker_cannot_journal_or_broadcast(pilot, tmp_path, monkeypatch):
    c, app, h, keys, _, settings = pilot
    signer_settings(settings, tmp_path)
    chain = SigningChain(settings)
    sent = install_rpc(chain, app)
    first = Worker(app.state.database, chain, settings)
    op = c.post("/bond/faucet", headers=h[0]).json()["operation_id"]
    old = first.claim()[0]
    original = chain.faucet
    def race(wallet, *, persist, prepared=None):
        with app.state.database.transaction() as db:
            db.get(Job, op).lease_until = 0
        second = Worker(app.state.database, chain, settings)
        second.reconcile()
        fresh = next(j for j in second.claim() if j.id == op)
        monkeypatch.setattr(chain, "faucet", original)
        assert second.execute(fresh)
        return original(wallet, persist=persist, prepared=prepared)
    monkeypatch.setattr(chain, "faucet", race)
    assert first.execute(old) is False
    assert len(sent) == 1
    assert c.get(f"/operations/{op}", headers=h[0]).json()["status"] == "finalized"


def test_faucet_journal_must_match_recipient_and_amount(pilot, tmp_path):
    c, app, h, keys, _, settings = pilot
    signer_settings(settings, tmp_path)
    chain = SigningChain(settings)
    install_rpc(chain, app, fail_send=True)
    app.state.worker = Worker(app.state.database, chain, settings)
    op = c.post("/bond/faucet", headers=h[0]).json()["operation_id"]
    run_work(app)
    with app.state.database.transaction() as db:
        prepared = db.get(Job, op).payload["prepared"]
    def no_rpc(*args):
        raise AssertionError("Mismatched journal must fail before RPC")
    chain.rpc = no_rpc
    def no_persist(*args):
        raise AssertionError("Must never replace an existing faucet journal")
    with pytest.raises(ChainUnavailable, match="does not match"):
        chain.faucet(str(keys[1].pubkey()), persist=no_persist,
                     prepared={**prepared, "wallet": str(keys[1].pubkey())})
    settings.min_bond += 1
    with pytest.raises(ChainUnavailable, match="does not match"):
        chain.faucet(str(keys[0].pubkey()), persist=no_persist, prepared=prepared)
