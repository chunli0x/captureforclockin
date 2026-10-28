"""Crash windows, atomic acceptance, fencing and autonomous recovery."""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from sqlalchemy import select, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from seeker.chain import ChainUnavailable
from seeker.db import Actor, Job, Judgment, JudgmentPair, JudgmentBatch, Prepared
from seeker.outbox import enqueue, LostLease
from seeker.worker import Worker, MAX_ATTEMPTS
from test_judgment import pilot, pilot_database_url, seed_pair, run_work


@pytest.fixture
def ready(pilot):
    c, app, h, keys, chain, settings = pilot
    settings.liveness_required = False
    with app.state.database.transaction() as db:
        for key in keys:
            db.get(Actor, str(key.pubkey())).bonded = True
    return pilot


def accept(ready, *, k=1, choice="a"):
    c, app, h, *_ = ready
    pid = seed_pair(app, k=k)
    r = c.post(f"/judge/{pid}", headers=h[0], json={"choice": choice})
    assert r.status_code == 202, r.text
    return pid, r.json()


def test_acceptance_has_no_chain_io_and_wallet_scoped_progress(ready, monkeypatch):
    c, app, h, keys, chain, settings = ready
    for name in ("reserve_judgment", "resolve_judgment", "settle_skr", "post_batch",
                 "bond_warnings", "bond_vault_balance"):
        monkeypatch.setattr(chain, name, Mock(side_effect=AssertionError("HTTP called chain")))
    pid, body = accept(ready)
    with app.state.database.transaction() as db:
        j = db.get(Judgment, body["judgment_id"])
        job = db.get(Job, body["operation_id"])
        assert j.choice == "a" and not j.liability_reserved
        assert job.entity_id == j.judgment_id and job.status == "accepted"
    for path in (f'/judgments/{body["judgment_id"]}', f'/operations/{body["operation_id"]}'):
        assert c.get(path, headers=h[0]).json()["status"] == "accepted"
        assert c.get(path, headers=h[1]).status_code == 404
        assert c.get(path).status_code == 401


def test_enqueue_failure_rolls_back_decision_and_key(ready, monkeypatch):
    from seeker import api
    c, app, h, *_ = ready
    pid = seed_pair(app)
    def crash(*args, **kwargs):
        enqueue(*args, **kwargs)
        raise RuntimeError("death before commit")
    monkeypatch.setattr(api, "enqueue", crash)
    with pytest.raises(RuntimeError):
        c.post(f"/judge/{pid}", headers={**h[0], "Idempotency-Key": "one"}, json={"choice": "a"})
    with app.state.database.transaction() as db:
        assert not list(db.scalars(select(Judgment)))
        assert not list(db.scalars(select(Job)))
    monkeypatch.setattr(api, "enqueue", enqueue)
    assert c.post(f"/judge/{pid}", headers={**h[0], "Idempotency-Key": "one"}, json={"choice": "b"}).status_code == 202


def test_process_death_after_acceptance_commit_needs_no_enqueue_or_user(ready):
    c, app, h, keys, chain, settings = ready
    pid = seed_pair(app, k=1)
    # A real child process exits immediately after the durable response. There
    # is intentionally no enqueue step, worker startup or cleanup after commit.
    script = '''
import os, sys
from unittest.mock import Mock
from fastapi.testclient import TestClient
from seeker.api import create_app
from seeker.config import Settings
from solders.keypair import Keypair
app = create_app(Settings(database_url=sys.argv[1], local_sqlite=True, liveness_required=False,
                         program_id=str(Keypair().pubkey()), skr_mint=str(Keypair().pubkey()),
                         verifier_key_file="unused"), chain=Mock())
r = TestClient(app).post('/judge/' + sys.argv[2], headers={'Authorization': sys.argv[3]}, json={'choice':'a'})
os._exit(77 if r.status_code == 202 else 78)
'''
    result = subprocess.run([sys.executable, "-c", script, settings.database_url, pid, h[0]["Authorization"]])
    assert result.returncode == 77
    with app.state.database.transaction() as db:
        assert db.scalar(select(Judgment)).pair_id == pid
        assert db.scalar(select(Job)).type == "reservation"
    run_work(app)
    with app.state.database.transaction() as db:
        assert db.get(JudgmentPair, pid).status == "resolved"
    assert len(chain.settled_skr) == 1


def test_duplicate_changing_and_concurrent_idempotency_keys(ready):
    c, app, h, *_ = ready
    pid = seed_pair(app)
    def post(key, body={"choice": "a"}, pair=pid):
        return c.post(f"/judge/{pair}", headers={**h[0], "Idempotency-Key": key}, json=body)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(post, ["same"] * 6))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["operation_id"] for r in responses}) == 1
    assert post("changed-key").json() == responses[0].json()
    assert post("same", {"choice": "b"}).status_code == 409
    assert post("changed-key", {"choice": "b"}).status_code == 409
    assert post("brand-new", {"choice": "b"}).status_code == 409
    assert post("same", pair=seed_pair(app)).status_code == 409
    with app.state.database.transaction() as db:
        assert len(list(db.scalars(select(Judgment)))) == 1
        assert len(list(db.scalars(select(Job)))) == 1


class ProcessDeath(BaseException):
    """Bypasses worker Exception handlers, like an abruptly terminated process."""


@pytest.mark.parametrize("payout_mode", ["mint", "transfer"])
@pytest.mark.parametrize("window", ["signed", "sent", "finalized_before_commit"])
def test_payout_crash_windows_resume_identical_obligation(ready, monkeypatch, window, payout_mode):
    c, app, h, keys, chain, settings = ready
    method = "settle_transfer" if payout_mode == "transfer" else "settle_skr"
    if payout_mode == "transfer":
        settings.payout_mode = "transfer"
        chain.settle_transfer = chain.settle_skr
    pid, body = accept(ready)
    worker = app.state.worker
    worker.run_once()
    lease = next(j for j in worker.claim() if j.type == "settlement")
    original = getattr(chain, method)
    if window == "signed":
        def crash(wallet, amount, memo, *, persist, prepared=None):
            persist({"transaction": "SIGNED-TEST-TX", "signature": "durable-signature", "last_valid_height": 100})
            raise ProcessDeath()
        setattr(chain, method, crash)
    elif window == "sent":
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise ProcessDeath()
        setattr(chain, method, crash)
    else:
        def before_commit(session):
            if any(isinstance(row, Judgment) and row.payout_signature for row in session.dirty):
                raise ProcessDeath()
        event.listen(app.state.database.factory, "before_commit", before_commit)
    with pytest.raises(ProcessDeath):
        worker.execute(lease)
    if window == "finalized_before_commit":
        event.remove(app.state.database.factory, "before_commit", before_commit)
    with app.state.database.transaction() as db:
        j = db.get(Judgment, body["judgment_id"])
        assert j.settling and j.payout_signature is None
        assert j.payout_transaction == "SIGNED-TEST-TX"
        assert db.get(Job, lease.id).status == "running"
        db.get(Job, lease.id).lease_until = 0
    def resume(wallet, amount, memo, *, persist, prepared=None):
        assert prepared and prepared["transaction"] == "SIGNED-TEST-TX"
        return original(wallet, amount, memo, persist=persist, prepared=prepared)
    setattr(chain, method, resume)
    # Replacement worker, no HTTP retry.
    app.state.worker = Worker(app.state.database, chain, settings, batch_size=100)
    run_work(app)
    assert len(chain.settled_skr) == 1
    with app.state.database.transaction() as db:
        assert db.get(JudgmentPair, pid).status == "resolved"
        assert db.get(Actor, str(keys[0].pubkey())).judged_count == 1
        assert db.get(Job, lease.id).status == "finalized"
    view = c.get(f'/operations/{body["operation_id"]}', headers=h[0]).json()
    assert view["status"] == view["payout"]["status"] == "finalized"


def test_expired_lease_blocks_late_ack_and_every_journal_write(ready):
    c, app, h, keys, chain, settings = ready
    _, body = accept(ready)
    first = app.state.worker
    old = first.claim()[0]
    with app.state.database.transaction() as db:
        db.get(Job, old.id).lease_until = 0
    second = Worker(app.state.database, chain, settings)
    second.reconcile()
    fresh = second.claim()[0]
    assert fresh.fencing_version > old.fencing_version
    assert not first.finish(old)
    with pytest.raises(LostLease):
        with first.transaction(old) as db:
            db.get(Judgment, body["judgment_id"]).liability_reserved = True
    assert second.execute(fresh)
    assert not first.execute(old)


def test_late_worker_cannot_overwrite_payout_journal(ready):
    c, app, h, keys, chain, settings = ready
    _, body = accept(ready)
    first = app.state.worker
    first.run_once()
    old = next(j for j in first.claim() if j.type == "settlement")
    original = chain.settle_skr
    def race(wallet, amount, memo, *, persist, prepared=None):
        with app.state.database.transaction() as db:
            db.get(Job, old.id).lease_until = 0
        second = Worker(app.state.database, chain, settings)
        second.reconcile()
        fresh = next(j for j in second.claim() if j.id == old.id)
        chain.settle_skr = original
        assert second.execute(fresh)
        persist({"transaction": "STALE", "signature": "STALE", "last_valid_height": 0})
        raise AssertionError("stale persist must fail")
    chain.settle_skr = race
    assert first.execute(old) is False
    with app.state.database.transaction() as db:
        j = db.get(Judgment, body["judgment_id"])
        assert j.payout_transaction == "SIGNED-TEST-TX" and not j.settling
    assert len(chain.settled_skr) == 1


@pytest.mark.parametrize("state", ["unreserved", "payout", "enforcement", "anchor", "prepared"])
def test_reconciler_recovers_stranded_states_without_http(ready, state):
    c, app, h, keys, chain, settings = ready
    pid = seed_pair(app, k=1)
    wallet = str(keys[0].pubkey())
    with app.state.database.transaction() as db:
        pair = db.get(JudgmentPair, pid)
        if state == "prepared":
            db.get(Actor, wallet).bonded = False
            p = Prepared(wallet=wallet, action="deposit", target=wallet,
                         message_hash="a" * 64, transaction="test", last_valid_height=100)
            db.add(p)
            db.flush()
            prepared_id = p.id
        else:
            j = Judgment(pair_id=pid, wallet=wallet, choice="a")
            db.add(j)
            db.flush()
            if state != "unreserved":
                j.liability_reserved, j.aligned = True, True
                j.earned_skr = settings.judge_base_skr
                pair.preference, pair.agreement, pair.confidence = "a", 1.0, 1.0
                if state == "payout":
                    j.settling, pair.status = True, "resolving"
                else:
                    j.payout_signature = "already-paid"
                    j.enforcement_requested = True
                    pair.status = "enforcing"
                    db.get(Actor, wallet).judged_count = 1
                    if state == "anchor":
                        j.enforcement_complete, pair.status = True, "resolved"
    assert not chain.settled_skr
    run_work(app)
    with app.state.database.transaction() as db:
        if state == "prepared":
            assert db.get(Prepared, prepared_id).confirmed and db.get(Actor, wallet).bonded
        else:
            assert db.get(JudgmentPair, pid).status == "resolved"
            assert db.get(JudgmentBatch, pair.batch_id).anchored
            assert db.get(Actor, wallet).judged_count == 1
    assert len(chain.settled_skr) == (1 if state in ("unreserved", "payout") else 0)


def test_bounded_retries_dead_letters_redacted_and_visible(ready, caplog):
    c, app, h, keys, chain, settings = ready
    _, body = accept(ready)
    chain.reserve_judgment = Mock(side_effect=ChainUnavailable("https://rpc/?secret=PRIVATE signed=BYTES"))
    worker = app.state.worker
    for _ in range(MAX_ATTEMPTS):
        worker.run_once()
        with app.state.database.transaction() as db:
            job = db.get(Job, body["operation_id"])
            if job.status != "failed":
                assert job.next_attempt_at > job.updated_at
                job.next_attempt_at = 0
    worker.reconcile()
    assert worker.claim() == []
    view = c.get(f'/operations/{body["operation_id"]}', headers=h[0]).json()
    assert view["status"] == view["payout"]["status"] == "failed"
    assert view["jobs"][0]["attempts"] == MAX_ATTEMPTS
    assert "PRIVATE" not in str(view) + caplog.text
    assert "dead-letter" in caplog.text.lower()


def test_network_never_runs_in_db_transaction_including_prepared_and_bonds(ready):
    c, app, h, keys, chain, settings = ready
    active = set()
    event.listen(app.state.database.engine, "begin", lambda conn: active.add(id(conn)))
    event.listen(app.state.database.engine, "commit", lambda conn: active.discard(id(conn)))
    event.listen(app.state.database.engine, "rollback", lambda conn: active.discard(id(conn)))
    for name in ("reserve_judgment", "resolve_judgment", "settle_skr", "post_batch", "faucet",
                 "prepare", "height", "confirm", "find_signature", "bond_warnings"):
        original = getattr(chain, name)
        def checked(*args, _original=original, **kwargs):
            assert not active, "chain I/O while a DB transaction is open"
            return _original(*args, **kwargs)
        setattr(chain, name, checked)
    c.post("/bond/faucet", headers=h[0])
    c.post("/bond/deposit", headers=h[0])
    c.post("/bond/deposit", headers=h[0])
    accept(ready)
    run_work(app)
    assert len(chain.settled_skr) == 1 and chain.posted


def test_two_workers_claim_disjoint_jobs_and_postgres_ddl(ready):
    _, app, *_rest, chain, settings = ready
    with app.state.database.transaction() as db:
        for i in range(8):
            enqueue(db, "reservation", str(i))
    workers = [Worker(app.state.database, chain, settings) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda w: w.claim(), workers))
    assert len(claimed[0]) == len(claimed[1]) == 4
    assert not {j.id for j in claimed[0]} & {j.id for j in claimed[1]}
    pg = str(CreateTable(Job.__table__).compile(dialect=postgresql.dialect()))
    local = str(CreateTable(Job.__table__).compile(dialect=sqlite.dialect()))
    assert "payload JSONB" in pg and "payload TEXT" in local
    assert "UNIQUE (type, entity_id, operation_version)" in pg


def test_claim_query_really_uses_skip_locked_on_postgres(ready):
    from contextlib import contextmanager
    _, app, *_rest, chain, settings = ready
    session = Mock()
    session.scalars.return_value = []
    @contextmanager
    def transaction():
        yield session
    worker = Worker(Mock(transaction=transaction), chain, settings)
    assert worker.claim() == []
    sql = str(session.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql and "LIMIT" in sql


def test_existing_enqueue_does_not_write_or_wait_on_job_row(ready):
    _, app, *_ = ready
    with app.state.database.transaction() as db:
        first = enqueue(db, "settlement", "pair")
    statements = []
    def capture(conn, cursor, sql, *args):
        statements.append(sql)
    event.listen(app.state.database.engine, "before_cursor_execute", capture)
    try:
        with app.state.database.transaction() as db:
            assert enqueue(db, "settlement", "pair").id == first.id
    finally:
        event.remove(app.state.database.engine, "before_cursor_execute", capture)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE")) for sql in statements)


def test_legacy_neutral_and_finalized_history_recover_without_rescoring(ready):
    _, app, _, keys, chain, settings = ready
    wallet = str(keys[0].pubkey())
    for historical in (False, True):
        pid = seed_pair(app)
        with app.state.database.transaction() as db:
            pair = db.get(JudgmentPair, pid)
            db.add(Judgment(pair_id=pid, wallet=wallet, choice="a" if historical else "tie",
                            consumed=not historical, liability_legacy=True))
            if historical:
                pair.status, pair.preference = "resolved", "a"
    run_work(app)
    with app.state.database.transaction() as db:
        assert all(j.enforcement_complete and not j.enforcement_penalty for j in db.scalars(select(Judgment)))
        assert db.get(Actor, wallet).judged_count == 0
    assert not chain.issued and not chain.settled_skr
