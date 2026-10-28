"""Transactional enqueue, fenced leases, and public operation projections."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import Job, JudgmentBatch, JudgmentPair, now, uid

from .schemas import OperationState

class LostLease(Exception):
    pass


class Waiting(Exception):
    """A durable dependency is incomplete; this is not a failed attempt."""


def enqueue(db, kind, entity_id, *, payload=None, version=1):
    logical = f"{kind}:{entity_id}:{version}"
    # Existing jobs need no write/lock. In particular, do not wait on a running
    # job's unique index tuple while holding its entity lock: the worker may
    # hold that job's fence lock and be waiting for the same entity.
    existing = db.scalar(select(Job).where(Job.logical_id == logical))
    if existing is not None:
        return existing
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(insert(Job).values(
        id=uid(), logical_id=logical, type=kind, entity_id=entity_id,
        operation_version=version, payload=payload or {}, status="accepted",
        attempts=0, next_attempt_at=now(), fencing_version=0,
        created_at=now(), updated_at=now(),
    ).on_conflict_do_nothing())
    return db.scalar(select(Job).where(Job.logical_id == logical))


def guard(db, lease):
    """Fence *every* journal write, not just the final job acknowledgement."""
    row = db.scalar(select(Job).where(
        Job.id == lease.id, Job.status == "running",
        Job.lease_owner == lease.lease_owner,
        Job.fencing_version == lease.fencing_version,
        Job.lease_until > now(),
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise LostLease()
    return row


def job_view(job):
    return {"id": job.id, "type": job.type, "status": (
        "pending" if job.status == "running" else job.status
    ), "attempts": job.attempts, "last_error": job.last_error}


def judgment_view(db, j):
    pair = db.get(JudgmentPair, j.pair_id)
    batch = db.get(JudgmentBatch, pair.batch_id)
    jobs = list(db.scalars(select(Job).where(
        ((Job.type == "reservation") & (Job.entity_id == j.judgment_id))
        | ((Job.type == "settlement") & (Job.entity_id == j.pair_id))
        | ((Job.type == "anchor") & (Job.entity_id == pair.batch_id))
    )))
    reservation = next((job for job in jobs if job.type == "reservation"), None)
    failed = any(job.status == "failed" for job in jobs)
    status = (OperationState.finalized if j.enforcement_complete and (j.consumed or batch.anchored)
              else OperationState.failed if failed
              else OperationState.accepted if not j.liability_reserved and (not reservation or reservation.status == "accepted")
              else OperationState.pending)
    payout = (OperationState.finalized if j.payout_signature or (j.enforcement_complete and not j.settling)
              else OperationState.failed if failed
              else OperationState.pending if j.settling or j.liability_reserved
              else OperationState.accepted)
    return {"judgment_id": j.judgment_id, "operation_id": reservation.id if reservation else None,
            "pair_id": j.pair_id, "status": status,
            "pair_status": pair.status, "preference": pair.preference,
            "agreement": pair.agreement, "confidence": pair.confidence,
            "payout": {"status": payout, "earned_skr": j.earned_skr,
                       "signature": j.payout_signature},
            "jobs": [job_view(job) for job in jobs]}
