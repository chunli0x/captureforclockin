"""Freshness and device-integrity trust boundaries, including HTTP gates."""

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair

from seeker.api import create_app
from seeker.attestation import (
    AttestationError, AttestationVerifier, DeviceIntegrity, LivenessVerification,
    verify_wallet_proof,
)
from seeker.db import Actor, now
from test_judgment import pilot, bond, seed_pair


def complete(c, headers, wallet, digest="12" * 32, **extra):
    session = c.post("/liveness/session", headers=headers, json={
        "contributor": wallet, "task_id": "freshness-test",
    })
    assert session.status_code == 200, session.text
    return c.post("/liveness/complete", headers=headers, json={
        "contributor": wallet, "session_id": session.json()["session_id"],
        "result_digest": digest, **extra,
    })


def test_wallet_signed_proof_binds_wallet_message_and_expiry():
    key = Keypair()
    args = dict(wallet=str(key.pubkey()), message=b"challenge", now=10, expires_at=20,
                signature=str(key.sign_message(b"challenge")))
    verify_wallet_proof(**args)
    for change in ({"wallet": str(Keypair().pubkey())}, {"message": b"other"},
                   {"signature": "garbage"}, {"now": 20}):
        with pytest.raises(AttestationError):
            verify_wallet_proof(**(args | change))


@pytest.mark.parametrize("method", ["motion", "demo", "attestation"])
def test_freshness_boundary(method):
    record = LivenessVerification(100, method, 200, "test-provider")
    assert record.is_fresh(now=100, demo_allowed=True)
    assert record.is_fresh(now=199, demo_allowed=True)
    assert not record.is_fresh(now=99, demo_allowed=True)
    assert not record.is_fresh(now=200, demo_allowed=True)
    assert not record.is_fresh(now=201, demo_allowed=True)
    assert record.is_fresh(now=150, demo_allowed=False) == (method != "demo")


@pytest.mark.parametrize("record", [
    LivenessVerification(None, "motion", 200),
    LivenessVerification(100, "motion", None),
    LivenessVerification(100, "invented", 200),
    LivenessVerification(100, "attestation", 200),
])
def test_incomplete_or_unknown_verification_fails_closed(record):
    assert not record.is_fresh(now=150, demo_allowed=True)


def test_demo_is_off_by_default_and_cannot_be_self_declared(pilot):
    c, app, headers, keys, chain, settings = pilot
    wallet = str(keys[0].pubkey())
    assert settings.liveness_demo is False
    assert c.get("/health").json()["liveness_demo"] is False
    response = complete(c, headers[0], wallet, "deadbeef" * 8)
    assert response.status_code == 400
    assert "demo liveness is disabled" in response.json()["detail"]
    assert c.get("/me", headers=headers[0]).json()["liveness_method"] is None
    assert complete(c, headers[0], wallet, liveness_method="attestation").status_code == 422
    settings.liveness_demo = True
    response = complete(c, headers[0], wallet, "deadbeef" * 8)
    assert response.status_code == 200, response.text
    assert response.json()["liveness_method"] == "demo"
    assert "no motion verified" in response.json()["attests"]
    assert c.get("/health").json()["liveness_demo"] is True
    assert c.get("/me", headers=headers[0]).json()["liveness_fresh"] is True
    # Disabling demo invalidates existing demo evidence immediately.
    settings.liveness_demo = False
    me = c.get("/me", headers=headers[0]).json()
    assert me["liveness_method"] == "demo"
    assert me["liveness_fresh"] is False
    assert me["liveness_completed"] is False
    assert c.get("/judge/next", headers=headers[0]).json()["kind"] == "none"
    pid = seed_pair(app)
    assert c.post(f"/judge/{pid}", headers=headers[0], json={"choice": "a"}).status_code == 403


def test_record_survives_app_recreation_and_expires_at_both_gates(pilot):
    c, app, headers, keys, chain, settings = pilot
    wallet = str(keys[0].pubkey())
    bond(c, headers[0])
    pid = seed_pair(app)
    response = complete(c, headers[0], wallet)
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["task_id"] == "freshness-test"
    assert record["liveness_method"] == "motion"
    assert record["liveness_device_integrity"] == "none"
    replacement = create_app(settings, chain=chain)
    try:
        with TestClient(replacement) as restarted:
            me = restarted.get("/me", headers=headers[0]).json()
            assert me["liveness_verified_at"] == record["liveness_verified_at"]
            assert me["liveness_fresh"] is True
            assert restarted.get("/judge/next", headers=headers[0]).json()["pair_id"] == pid
            # Advance only the liveness record's clock, keeping auth valid.
            with replacement.state.database.transaction() as db:
                actor = db.get(Actor, wallet)
                actor.liveness_expires_at = now()
                actor.liveness_completed = True  # Old flags cannot override expiry.
            me = restarted.get("/me", headers=headers[0]).json()
            assert me["liveness_fresh"] is False
            assert me["liveness_completed"] is False
            assert me["liveness_method"] == "motion"
            assert restarted.get("/judge/next", headers=headers[0]).json()["kind"] == "none"
            assert restarted.post(f"/judge/{pid}", headers=headers[0], json={"choice": "a"}).status_code == 403
            assert complete(restarted, headers[0], wallet).status_code == 200
            assert restarted.get("/judge/next", headers=headers[0]).json()["pair_id"] == pid
    finally:
        replacement.state.database.engine.dispose()


def test_unconfigured_device_token_is_rejected(pilot):
    c, app, headers, keys, chain, settings = pilot
    response = complete(c, headers[0], str(keys[0].pubkey()), device_integrity_token="untrusted")
    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]
    assert c.get("/me", headers=headers[0]).json()["liveness_fresh"] is False


@pytest.mark.parametrize("expires_in", [0, -1, 30])
def test_injected_device_provider_binds_proof_and_caps_expiry(pilot, expires_in):
    c, app, headers, keys, chain, settings = pilot
    wallet = str(keys[0].pubkey())
    seen = []

    class TestProvider:
        def verify(self, **context):
            seen.append(context)
            assert context["wallet"] == wallet
            assert context["token"] == "provider-token"
            return DeviceIntegrity("test-integrity", context["now"] + expires_in)

    replacement = create_app(settings, chain=chain,
                             attestation_verifier=AttestationVerifier(TestProvider()))
    try:
        with TestClient(replacement) as client:
            response = complete(client, headers[0], wallet, device_integrity_token="provider-token")
            if expires_in <= 0:
                assert response.status_code == 400
                assert "expired" in response.json()["detail"]
                assert client.get("/me", headers=headers[0]).json()["liveness_fresh"] is False
            else:
                assert response.status_code == 200, response.text
                record = response.json()
                assert record["liveness_method"] == "attestation"
                assert record["liveness_device_integrity"] == "test-integrity"
                assert bytes.fromhex(record["liveness_proof_hash"]) == seen[0]["proof_hash"]
                assert record["liveness_expires_at"] == seen[0]["now"] + expires_in
                assert client.get("/me", headers=headers[0]).json()["liveness_method"] == "attestation"
    finally:
        replacement.state.database.engine.dispose()


def test_environment_controls_demo_and_verification_lifetime(pilot, monkeypatch):
    from seeker.config import Settings
    from pydantic import ValidationError

    settings = pilot[-1]
    values = settings.model_dump(exclude={"liveness_demo", "liveness_verification_seconds"})
    monkeypatch.setenv("SEEKER_LIVENESS_DEMO", "true")
    monkeypatch.setenv("SEEKER_LIVENESS_VERIFICATION_SECONDS", "90")
    assert Settings(**values).liveness_demo is True
    assert Settings(**values).liveness_verification_seconds == 90
    monkeypatch.setenv("SEEKER_LIVENESS_VERIFICATION_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(**values)
