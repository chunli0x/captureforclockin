from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from solders.pubkey import Pubkey


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Wallet(Strict):
    wallet: str

    @field_validator("wallet")
    @classmethod
    def valid_wallet(cls, v):
        Pubkey.from_string(v)
        return v


class Verify(Strict):
    challenge_id: str = Field(min_length=32, max_length=64)
    signature: str = Field(min_length=64, max_length=88)
    over_18: bool = False


class LivenessSession(Strict):
    """Start a liveness session: a fresh motion challenge before the first
    judgment. ``contributor`` is the wallet; ``task_id`` binds the proof to a
    task."""

    contributor: str
    task_id: str = Field(min_length=1, max_length=64)

    @field_validator("contributor")
    @classmethod
    def valid_wallet(cls, v):
        Pubkey.from_string(v)
        return v


class LivenessComplete(Strict):
    """Finish a liveness session: the on-device check's opaque result digest.

    ``contributor`` is the wallet; ``session_id``/``result_digest`` come from
    the challenge issued by /liveness/session. No biometric material arrives.
    """

    contributor: str
    session_id: str = Field(min_length=1, max_length=64)
    result_digest: str = Field(min_length=2, max_length=256)

    @field_validator("contributor")
    @classmethod
    def valid_wallet(cls, v):
        Pubkey.from_string(v)
        return v


class Confirm(Strict):
    signature: str = Field(min_length=64, max_length=88)


class JudgmentSubmit(Strict):
    """One judgment: a preference choice or a 1-5 rating.

    ``choice`` is "a" | "b" | "tie" | "cant_tell" for preference tasks; "tie"
    and "cant_tell" are neutral (non-voting). ``rating`` is 1..5 for rating
    tasks. The endpoint validates the right field against the pair's task type;
    here only bounds are enforced."""

    choice: Literal["a", "b", "tie", "cant_tell"] | None = None
    rating: int | None = Field(default=None, ge=1, le=5)


class OperationState(str, Enum):
    accepted = "accepted"
    pending = "pending"
    finalized = "finalized"
    failed = "failed"


class JudgmentAccepted(BaseModel):
    status: Literal["accepted"]
    judgment_id: str
    operation_id: str


class PayoutView(BaseModel):
    status: OperationState
    earned_skr: int
    signature: str | None


class OperationJobView(BaseModel):
    id: str
    type: str
    status: OperationState
    attempts: int
    last_error: str | None


class JudgmentOperationView(BaseModel):
    judgment_id: str
    operation_id: str | None
    pair_id: str
    status: OperationState
    pair_status: str
    preference: str | None
    agreement: float | None
    confidence: float | None
    payout: PayoutView
    jobs: list[OperationJobView]
