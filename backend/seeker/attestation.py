"""Wallet proof verification and a replaceable device-integrity boundary.

Wallet signatures prove ownership, not motion. Liveness uses the existing
signature-authenticated session; its digest remains an on-device assertion.
A provider must authenticate its token, app identity, wallet and proof hash
(including the server nonce), and enforce its own replay policy. Client claims
must never be returned as verified device integrity without provider checks.
"""

from dataclasses import dataclass
from typing import Protocol

from solders.pubkey import Pubkey
from solders.signature import Signature

from .liveness import DEMO_RESULT_DIGEST, LivenessError


class AttestationError(LivenessError):
    pass


def verify_wallet_proof(*, wallet: str, message: bytes, signature: str,
                        expires_at: int, now: int) -> None:
    if now >= expires_at:
        raise AttestationError("Wallet proof expired")
    try:
        valid = Signature.from_string(signature).verify(Pubkey.from_string(wallet), message)
    except ValueError:
        valid = False
    if not valid:
        raise AttestationError("Signature does not match this wallet and challenge")


@dataclass(frozen=True)
class DeviceIntegrity:
    signal: str = "none"
    expires_at: int | None = None


class DeviceIntegrityVerifier(Protocol):
    def verify(self, *, wallet: str, proof_hash: bytes, token: str | None,
               now: int) -> DeviceIntegrity: ...


class NoDeviceIntegrity:
    def verify(self, *, wallet: str, proof_hash: bytes, token: str | None,
               now: int) -> DeviceIntegrity:
        if token is not None:
            raise AttestationError("Device integrity provider is not configured")
        return DeviceIntegrity()


@dataclass(frozen=True)
class LivenessVerification:
    verified_at: int | None
    method: str | None
    expires_at: int | None
    device_integrity: str | None = "none"

    def is_fresh(self, *, now: int, demo_allowed: bool) -> bool:
        return (
            self.method in {"demo", "motion", "attestation"}
            and (self.method != "demo" or demo_allowed)
            and self.verified_at is not None
            and self.expires_at is not None
            and self.verified_at <= now < self.expires_at
            and (self.method != "attestation" or self.device_integrity not in {None, "none", ""})
        )

    @classmethod
    def from_actor(cls, actor):
        return cls(actor.liveness_verified_at, actor.liveness_method,
                   actor.liveness_expires_at, actor.liveness_device_integrity)


class AttestationVerifier:
    def __init__(self, device_integrity: DeviceIntegrityVerifier | None = None):
        self.device_integrity = device_integrity or NoDeviceIntegrity()

    def verify_liveness(self, *, wallet: str, proof_hash: bytes,
                        result_digest: bytes, device_token: str | None,
                        now: int, ttl_seconds: int, demo_allowed: bool) -> LivenessVerification:
        # Called only after wallet-session authentication and single-use
        # challenge completion. There is no client-selectable method field.
        demo = result_digest == DEMO_RESULT_DIGEST
        if demo and not demo_allowed:
            raise AttestationError("demo liveness is disabled; complete the motion check")
        integrity = self.device_integrity.verify(
            wallet=wallet, proof_hash=proof_hash, token=device_token, now=now,
        )
        expires_at = now + ttl_seconds
        if integrity.signal != "none":
            if not integrity.signal or len(integrity.signal) > 64:
                raise AttestationError("Invalid device integrity signal")
            if integrity.expires_at is None or integrity.expires_at <= now:
                raise AttestationError("Device integrity verification expired")
            expires_at = min(expires_at, integrity.expires_at)
        elif device_token is not None:
            raise AttestationError("Device integrity token was not verified")
        verification = LivenessVerification(
            now, "demo" if demo else ("attestation" if integrity.signal != "none" else "motion"),
            expires_at, integrity.signal,
        )
        if not verification.is_fresh(now=now, demo_allowed=demo_allowed):
            raise AttestationError("Liveness verification expired")
        return verification
