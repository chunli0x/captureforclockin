"""Startup must be read-only; failed requests must spend durable budgets."""

import asyncio
import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import OperationalError

from seeker.api import create_app
from seeker.config import Settings
from seeker.db import Actor, Base, Database, Login, SchemaVersion, now
from seeker import migrate
from seeker.rate_limit import RateLimiter, RequestLimitsMiddleware


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Admission tests must not straddle a real fixed-window boundary.
    timestamp = now()
    monkeypatch.setattr("seeker.rate_limit.now", lambda: timestamp)
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'durability.db'}", local_sqlite=True,
        program_id=str(Keypair().pubkey()), skr_mint=str(Keypair().pubkey()),
        verifier_key_file=tmp_path / 'unused',
    )


@pytest.fixture
def database(settings):
    database = Database(settings.database_url)
    yield database
    database.engine.dispose()


def statements(engine):
    seen = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, *args: seen.append(sql))
    return seen


def assert_no_writes(seen):
    assert not any(sql.lstrip().split()[0].upper() in {
        "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE"
    } for sql in seen)


def test_empty_startup_fails_without_creating_tables(settings, database):
    with pytest.raises(RuntimeError, match="schema not applied — run migrations"):
        create_app(settings, chain=Mock())
    assert inspect(database.engine).get_table_names() == []


def test_migrator_cli_records_checksums_and_second_run_is_noop(settings, database):
    env = dict(os.environ, SEEKER_DATABASE_URL=settings.database_url, SEEKER_LOCAL_SQLITE="true",
               SEEKER_PROGRAM_ID=settings.program_id, SEEKER_SKR_MINT=settings.skr_mint,
               SEEKER_VERIFIER_KEY_FILE=str(settings.verifier_key_file))
    first = subprocess.run([sys.executable, "-m", "seeker.migrate"], env=env, capture_output=True, text=True, check=True)
    assert "7 migration version(s) recorded" in first.stdout
    with database.engine.connect() as conn:
        before = conn.execute(select(SchemaVersion.__table__)).all()
    assert [r.version for r in before] == ["0011", "0012", "0013", "0014", "0015", "0016", "0017"]
    for row, (version, checksum, _) in zip(before, migrate._numbered_migrations()):
        assert row.version == version and row.checksum == checksum
        assert row.build_family == migrate.BUILD_FAMILY and row.applied_at
    second = subprocess.run([sys.executable, "-m", "seeker.migrate"], env=env, capture_output=True, text=True, check=True)
    assert "0 migration version(s) recorded" in second.stdout
    with database.engine.connect() as conn:
        assert conn.execute(select(SchemaVersion.__table__)).all() == before
    seen = statements(database.engine)
    assert migrate.apply_migrations(database.engine) == 0
    migrate.check_schema(database.engine)
    assert_no_writes(seen)
    app = create_app(settings, chain=Mock())
    app.state.database.engine.dispose()


def test_preledger_schema_is_baselined_and_balances_preserved(database):
    Base.metadata.create_all(database.engine)
    with database.transaction() as db:
        db.add(Actor(wallet="legacy-wallet", staked_skr=123))
    with database.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE schema_versions")
        conn.exec_driver_sql("ALTER TABLE seeker_actors DROP COLUMN faucet_count")
        conn.exec_driver_sql("ALTER TABLE judgments DROP COLUMN settling")
    assert migrate.apply_migrations(database.engine) == 7
    migrate.check_schema(database.engine)
    with database.transaction() as db:
        actor = db.get(Actor, "legacy-wallet")
        assert actor.staked_skr == 123 and actor.faucet_count == 0


@pytest.mark.parametrize("corruption, message", [
    ("DELETE FROM schema_versions WHERE version='0016'", "schema not applied — run migrations"),
    ("DELETE FROM schema_versions WHERE version='0011'", "schema not applied — run migrations"),
    ("UPDATE schema_versions SET checksum='tampered' WHERE version='0012'", "checksum mismatch"),
    ("UPDATE schema_versions SET build_family='OTHER'", "build family mismatch"),
    ("ALTER TABLE judgments DROP COLUMN settling", "schema not applied — run migrations"),
])
def test_startup_rejects_missing_or_corrupt_schema(settings, database, corruption, message):
    migrate.apply_migrations(database.engine)
    with database.engine.begin() as conn:
        conn.exec_driver_sql(corruption)
    seen = statements(database.engine)
    with pytest.raises(RuntimeError, match=message):
        migrate.check_schema(database.engine)
    with pytest.raises(RuntimeError, match=message):
        create_app(settings, chain=Mock())
    assert_no_writes(seen)
    if "UPDATE" in corruption:
        with pytest.raises(RuntimeError, match=message):
            migrate.apply_migrations(database.engine)
        assert_no_writes(seen)


@pytest.mark.parametrize("ledger", [True, False])
def test_other_build_cannot_start_or_migrate(database, monkeypatch, ledger):
    migrate.apply_migrations(database.engine)
    if not ledger:
        with database.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE schema_versions")
    other = "USDC" if migrate.BUILD_FAMILY == "SKR" else "SKR"
    monkeypatch.setattr(migrate, "BUILD_FAMILY", other)
    seen = statements(database.engine)
    for operation in (migrate.check_schema, migrate.apply_migrations):
        with pytest.raises(RuntimeError, match="build family mismatch"):
            operation(database.engine)
    assert_no_writes(seen)


def test_failed_migration_rolls_back_ddl_and_ledger(database, monkeypatch):
    migrations = migrate._numbered_migrations()
    migrations[-1][2].append(("judgments", "broken", "INVALID SQL !!!"))
    monkeypatch.setattr(migrate, "_numbered_migrations", lambda: migrations)
    with pytest.raises(OperationalError):
        migrate.apply_migrations(database.engine)
    assert inspect(database.engine).get_table_names() == []


def test_concurrent_migrators_record_each_version_once(database):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: migrate.apply_migrations(database.engine), range(4)))
    assert sorted(results) == [0, 0, 0, 7]
    migrate.check_schema(database.engine)


@pytest.fixture
def app(settings, database):
    migrate.apply_migrations(database.engine)
    app = create_app(settings, chain=Mock())
    yield app
    app.state.database.engine.dispose()


@pytest.mark.parametrize("path", ["/judge/missing", "/bond/deposit", "/liveness/complete", "/auth/verify"])
def test_invalid_requests_spend_budget_and_survive_app_restart(app, settings, path):
    settings.rate_ip_per_minute = 2
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post(path, json={}).status_code in (401, 422)
    second = create_app(settings, chain=Mock())
    with TestClient(second) as client:
        response = client.post(path, json={})
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 60
    second.state.database.engine.dispose()


def test_wallet_budget_shared_across_tokens_and_ips(app, database, settings):
    settings.rate_wallet_per_minute = 2
    wallet = str(Keypair().pubkey())
    with database.transaction() as db:
        db.add(Actor(wallet=wallet))
        db.flush()
        for token in ("one", "two", "expired"):
            db.add(Login(digest=hashlib.sha256(token.encode()).hexdigest(), wallet=wallet,
                         expires=now() + 3600 if token != "expired" else 0))
    for ip, token, status in [("ip1", "expired", 401), ("ip2", "two", 200), ("ip3", "one", 429)]:
        with TestClient(app, client=(ip, 1234)) as client:
            assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == status


def test_failed_signature_spends_wallet_budget(app, settings):
    settings.rate_wallet_per_minute = 2
    with TestClient(app) as client:
        key = Keypair()
        challenge = client.post("/auth/challenge", json={"wallet": str(key.pubkey())}).json()
        body = {"challenge_id": challenge["challenge_id"], "signature": str(Keypair().sign_message(b"wrong"))}
        assert client.post("/auth/verify", json=body).status_code == 401
        assert client.post("/auth/verify", json=body).status_code == 429


def test_global_budget_shared_across_ips(app, settings):
    settings.rate_global_per_minute = 2
    for index, expected in enumerate((401, 401, 429)):
        with TestClient(app, client=(f"ip{index}", 1234)) as client:
            assert client.get("/me").status_code == expected


def test_ip_budget_cannot_be_bypassed_with_forwarded_headers(app, settings):
    settings.rate_ip_per_minute = 1
    with TestClient(app) as client:
        assert client.get("/me", headers={"X-Forwarded-For": "ip1"}).status_code == 401
        assert client.get("/me", headers={"X-Forwarded-For": "ip2"}).status_code == 429


def test_atomic_budget_and_window_reset(database, monkeypatch):
    migrate.apply_migrations(database.engine)
    monkeypatch.setattr("seeker.rate_limit.now", lambda: 120)
    limiter = RateLimiter(database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.consume([("global", 5, 60)]), range(20)))
    assert results.count(0) == 5
    assert results.count(60) == 15
    monkeypatch.setattr("seeker.rate_limit.now", lambda: 180)
    assert limiter.consume([("global", 5, 60)]) == 0


def test_oversized_body_and_headers_spend_ingress_budget(app, settings):
    settings.max_request_body_bytes = 32
    settings.max_request_head_bytes = 512
    settings.rate_ip_per_minute = 2
    with TestClient(app) as client:
        assert client.post("/auth/verify", content=b"x" * 33).status_code == 413
        assert client.get("/me", headers={"X-Large": "x" * 512}).status_code == 431
        assert client.get("/me").status_code == 429


@pytest.mark.asyncio
async def test_stream_limit_without_content_length_and_slow_body(database, settings):
    migrate.apply_migrations(database.engine)
    settings.max_request_body_bytes = 8
    settings.request_body_timeout_seconds = 0.01
    downstream = Mock()
    middleware = RequestLimitsMiddleware(downstream, database, settings)
    scope = {"type": "http", "path": "/judge/test", "client": ("ip1", 1234), "headers": []}
    sent = []

    async def send(message):
        sent.append(message)

    chunks = iter([b"1234", b"56789"])

    async def chunked():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    await middleware(scope, chunked, send)
    assert sent[0]["status"] == 413
    sent.clear()

    async def slow():
        await asyncio.sleep(1)

    await middleware(scope, slow, send)
    assert sent[0]["status"] == 408
    downstream.assert_not_called()


def test_budget_store_failure_fails_closed(app):
    with app.state.database.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE seeker_rates")
    with TestClient(app) as client:
        assert client.get("/me").status_code == 503


@pytest.mark.parametrize("path, body, status", [
    ("/judge/missing", {"choice": "a"}, 404),
    ("/bond/deposit", {}, 409),
    ("/liveness/complete", {}, 422),
])
def test_authenticated_route_failures_spend_wallet_budget(app, database, settings, path, body, status):
    settings.rate_wallet_per_minute = 1
    wallet = str(Keypair().pubkey())
    with database.transaction() as db:
        db.add(Actor(wallet=wallet))
        db.flush()
        db.add(Login(digest=hashlib.sha256(b"token").hexdigest(), wallet=wallet, expires=now() + 3600))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer token"}
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == status, response.text
        assert client.post(path, json=body, headers=headers).status_code == 429


def test_body_parse_failure_spends_budget(app, settings):
    settings.rate_ip_per_minute = 1
    with TestClient(app) as client:
        assert client.post("/auth/verify", content=b"{", headers={"Content-Type": "application/json"}).status_code == 422
        assert client.post("/auth/verify", content=b"{").status_code == 429


def test_delayed_counter_cannot_reopen_old_window(database, monkeypatch):
    migrate.apply_migrations(database.engine)
    limiter = RateLimiter(database)
    monkeypatch.setattr("seeker.rate_limit.now", lambda: 180)
    assert limiter.consume([("global", 1, 60)]) == 0
    monkeypatch.setattr("seeker.rate_limit.now", lambda: 179)
    assert limiter.consume([("global", 1, 60)]) > 0
    monkeypatch.setattr("seeker.rate_limit.now", lambda: 180)
    assert limiter.consume([("global", 1, 60)]) == 60
