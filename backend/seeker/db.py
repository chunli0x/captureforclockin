"""Judgment product (RLHF preference judging) schema. Apply with
`python -m seeker.migrate`; no startup DDL."""

import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)


def uid():
    return uuid.uuid4().hex


def now():
    return int(time.time())


class Base(DeclarativeBase):
    pass


class Actor(Base):
    __tablename__ = "seeker_actors"
    wallet: Mapped[str] = mapped_column(String(44), primary_key=True)
    # Self-attested over-18 declaration recorded at enrollment. A claim, not a
    # cryptographic proof — never treated as one.
    over_18: Mapped[bool] = mapped_column(Boolean, default=False)
    # Server-side liveness gate. Set once a wallet completes a server-issued
    # liveness session (see /liveness/complete). Proof-of-human, not bank-grade.
    liveness_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bond gate (all server-recorded): bonded == the devnet bond was deposited;
    # skr_funded == the devnet test $SKR faucet ran.
    bonded: Mapped[bool] = mapped_column(Boolean, default=False)
    skr_funded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Number of times this wallet has tapped the devnet test $SKR faucet
    # (capped at faucet_max_mints). skr_funded stays as the "funded at least
    # once" flag for the deposit gate.
    faucet_count: Mapped[int] = mapped_column(Integer, default=0)
    # ---- Judgment product (RLHF preference judging) ----
    # calibrated == passed the N-pair calibration gate (unpaid) and may stake +
    # judge. agreement_rate == rolling fraction of resolved judgments this judge
    # was aligned on (last judge_window). judged_count == resolved judgments
    # participated in. staked_skr == the SKR bond escrowed on deposit (mirror).
    # warnings == mirror of the on-chain bond.warnings ladder (0 clean, 1 warned,
    # >= 2 slashed).
    calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    agreement_rate: Mapped[float] = mapped_column(Float, default=0.0)
    judged_count: Mapped[int] = mapped_column(Integer, default=0)
    staked_skr: Mapped[int] = mapped_column(BigInteger, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)


class Challenge(Base):
    __tablename__ = "seeker_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(String(44))
    message: Mapped[str] = mapped_column(String(1024))
    expires: Mapped[int] = mapped_column(BigInteger)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Login(Base):
    __tablename__ = "seeker_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(ForeignKey(Actor.wallet))
    expires: Mapped[int] = mapped_column(BigInteger)


class Rate(Base):
    __tablename__ = "seeker_rates"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer)


class Prepared(Base):
    __tablename__ = "seeker_prepared"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    wallet: Mapped[str] = mapped_column(ForeignKey(Actor.wallet), index=True)
    action: Mapped[str] = mapped_column(String(24))
    target: Mapped[str] = mapped_column(String(44), index=True)
    message_hash: Mapped[str] = mapped_column(String(64))
    transaction: Mapped[str] = mapped_column(String(4096))
    last_valid_height: Mapped[int] = mapped_column(BigInteger)
    signature: Mapped[str | None] = mapped_column(String(88), unique=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class JudgmentBatch(Base):
    """A batch of pairs whose resolved preferences commit one Merkle root
    on-chain (provenance anchor for the RLHF preference dataset)."""

    __tablename__ = "judgment_batches"
    batch_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    merkle_root: Mapped[str | None] = mapped_column(String(64))
    # anchored == the merkle_root was confirmed on-chain via post_batch. Set
    # only after a successful confirm so a failed final anchor is durably
    # retried (finding #10) instead of swallowed.
    anchored: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class JudgmentPair(Base):
    """One A/B preference or 1-5 rating task, sent to K judges."""

    __tablename__ = "judgment_pairs"
    pair_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey(JudgmentBatch.batch_id), index=True)
    media_type: Mapped[str] = mapped_column(String(16), default="image")
    task_type: Mapped[str] = mapped_column(String(16), default="preference")
    output_a_url: Mapped[str | None] = mapped_column(String(512))
    output_b_url: Mapped[str | None] = mapped_column(String(512))
    rubric: Mapped[str | None] = mapped_column(String(1024))
    k: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default="open")
    preference: Mapped[str | None] = mapped_column(String(8))
    agreement: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class Judgment(Base):
    """One judge's answer to one pair. One judge cannot answer a pair twice."""

    __tablename__ = "judgments"
    __table_args__ = (UniqueConstraint("pair_id", "wallet"),)
    judgment_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    pair_id: Mapped[str] = mapped_column(ForeignKey(JudgmentPair.pair_id), index=True)
    wallet: Mapped[str] = mapped_column(ForeignKey(Actor.wallet), index=True)
    choice: Mapped[str] = mapped_column(String(16))
    rating: Mapped[int | None] = mapped_column(Integer)
    aligned: Mapped[bool | None] = mapped_column(Boolean)
    # consumed == this judgment was already factored into a reroute (a neutral
    # "cant_tell"/"tie" vote). Consumed judgments do not re-trigger a reroute
    # and are excluded from the final consensus denominator.
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    earned_skr: Mapped[int] = mapped_column(BigInteger, default=0)
    payout_signature: Mapped[str | None] = mapped_column(String(88))
    # settling == the earned_skr obligation was committed to the ledger but its
    # mint transaction has not yet been confirmed on-chain. Payout obligations
    # are persisted BEFORE broadcast so a partial payout can never be lost or
    # double-paid (finding 10 #1); cleared once the mint confirms.
    settling: Mapped[bool] = mapped_column(Boolean, default=False)
    # judgment_id is also the unique, immutable on-chain resolution ID.
    liability_legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    liability_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    enforcement_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    enforcement_penalty: Mapped[bool] = mapped_column(Boolean, default=False)
    enforcement_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    payout_transaction: Mapped[str | None] = mapped_column(String(4096))
    payout_pending_signature: Mapped[str | None] = mapped_column(String(88))
    payout_last_valid_height: Mapped[int | None] = mapped_column(BigInteger)
    submitted_at: Mapped[int] = mapped_column(BigInteger, default=now)


class CalibrationPair(Base):
    """A known-consensus pair used to gate new judges before they can stake.

    ``known_choice`` is "a" | "b" for preference tasks, or the known rating as a
    decimal string (e.g. "4") for rating tasks."""

    __tablename__ = "calibration_pairs"
    pair_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    known_choice: Mapped[str] = mapped_column(String(16))
    known_consensus: Mapped[float] = mapped_column(Float, default=1.0)
    media_type: Mapped[str] = mapped_column(String(16), default="image")
    task_type: Mapped[str] = mapped_column(String(16), default="preference")
    output_a_url: Mapped[str | None] = mapped_column(String(512))
    output_b_url: Mapped[str | None] = mapped_column(String(512))
    rubric: Mapped[str | None] = mapped_column(String(1024))


class CalibrationAnswer(Base):
    """A judge's answer to a calibration pair; drives calibration progress."""

    __tablename__ = "calibration_answers"
    __table_args__ = (UniqueConstraint("pair_id", "wallet"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    pair_id: Mapped[str] = mapped_column(ForeignKey(CalibrationPair.pair_id), index=True)
    wallet: Mapped[str] = mapped_column(ForeignKey(Actor.wallet), index=True)
    choice: Mapped[str] = mapped_column(String(16))
    rating: Mapped[int | None] = mapped_column(Integer)
    correct: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[int] = mapped_column(BigInteger, default=now)


def connect(url):
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30}
        if url.startswith("sqlite")
        else {},
    )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def sqlite_connection(dbapi_connection, _):
            dbapi_connection.isolation_level = None
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def sqlite_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


class Database:
    def __init__(self, url):
        self.engine = connect(url)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.factory.begin() as session:
            yield session
