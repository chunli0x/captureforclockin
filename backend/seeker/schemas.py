from enum import Enum
from typing import Annotated, Literal

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
    device_integrity_token: str | None = Field(default=None, min_length=1, max_length=8192)

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


class FaucetOperationView(BaseModel):
    operation_id: str
    type: Literal["faucet"]
    status: OperationState
    funded: bool
    already: bool
    mints: int
    max: int
    signature: str | None
    last_error: str | None


OpaqueId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
MediaHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ApprovedVariant(Strict):
    variant_id: OpaqueId
    version: int = Field(ge=1)
    sha256: MediaHash
    duration_ms: int = Field(gt=0, le=4000)
    width: Literal[640]
    height: Literal[360]
    fps: Literal[24]
    content_type: Literal["video/mp4"] = "video/mp4"
    url: str
    expires_at: int


class VideoManifest(Strict):
    modality: Literal["video"] = "video"
    asset_id: OpaqueId
    version: int = Field(ge=1)
    sha256: MediaHash
    duration_ms: int = Field(gt=0, le=4000)
    width: Literal[640]
    height: Literal[360]
    approved_variants: list[ApprovedVariant] = Field(min_length=1, max_length=1)
    expires_at: int


class IngestView(Strict):
    asset_id: OpaqueId
    job_id: OpaqueId
    status: Literal["pending", "running", "review", "approved", "failed", "revoked"]
    sha256: MediaHash | None = None
    error_code: Literal["inspection_failed", "duplicate", "processing_failed"] | None = None


class MediaApproval(Strict):
    sha256: MediaHash
    # The reviewer must watch the entire local derivative, including frame stepping.
    watched_entire_clip: Literal[True]
    neutral_content: Literal[True]
    no_identifying_content: Literal[True]


class VideoCommission(Strict):
    asset_a_id: OpaqueId
    asset_b_id: OpaqueId


class VideoPairView(Strict):
    pair_id: OpaqueId
    task_type: Literal["preference"] = "preference"
    media_type: Literal["video"] = "video"
    modality: Literal["video"] = "video"
    output_a_url: str
    output_b_url: str
    rubric: Literal["Compare robot vision video outputs."] = "Compare robot vision video outputs."
    media_a: VideoManifest
    media_b: VideoManifest
