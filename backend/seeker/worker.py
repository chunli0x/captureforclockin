"""Durable execution: python -m seeker.worker. No startup DDL.

Leases are committed before dispatch. A late worker may finish chain I/O, whose
immutable receipts guard effects, but cannot write journals or acknowledge a job.
"""
import argparse
import logging
import random
import signal
import threading
from contextlib import contextmanager

from sqlalchemy import select, update, func, case

from .db import Database, Job, Judgment, JudgmentPair, JudgmentBatch, Prepared, now, uid
from .outbox import enqueue, guard, LostLease, Waiting

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


class Worker:
    def __init__(self, database, chain, settings, *, owner=None, lease_seconds=120, batch_size=4):
        self.database, self.chain, self.settings = database, chain, settings
        self.owner = owner or uid()
        self.lease_seconds, self.batch_size = lease_seconds, batch_size

    def claim(self):
        with self.database.transaction() as db:
            jobs = list(db.scalars(select(Job).where(
                Job.status.in_(("accepted", "pending")), Job.next_attempt_at <= now(),
                Job.attempts < MAX_ATTEMPTS,
            ).order_by(Job.next_attempt_at, Job.created_at, Job.id)
              .limit(self.batch_size).with_for_update(skip_locked=True)))
            for job in jobs:
                job.status, job.lease_owner = "running", self.owner
                job.lease_until = now() + self.lease_seconds
                job.fencing_version += 1
                job.attempts += 1
                job.updated_at = now()
            return jobs

    @contextmanager
    def transaction(self, lease):
        with self.database.transaction() as db:
            guard(db, lease)
            yield db

    def finish(self, lease, error=None):
        waiting = isinstance(error, Waiting)
        status = "finalized" if error is None else (
            "failed" if lease.attempts >= MAX_ATTEMPTS and not waiting else "pending")
        # Never persist/log exception text: RPC errors may include URLs, keys,
        # signed bytes or user content. Keep only a bounded category.
        from .chain import ChainUnavailable
        category = (None if error is None or waiting else
                    "chain_unavailable" if isinstance(error, ChainUnavailable) else "execution_error")
        delay = 5 if waiting else int(min(3600, 2 ** min(lease.attempts, 12)) + random.uniform(1, 5))
        with self.database.transaction() as db:
            result = db.execute(update(Job).where(
                Job.id == lease.id, Job.status == "running",
                Job.lease_owner == self.owner, Job.fencing_version == lease.fencing_version,
                Job.lease_until > now(),
            ).values(status=status, lease_owner=None, lease_until=None,
                     last_error=category, updated_at=now(), next_attempt_at=now() + delay,
                     attempts=Job.attempts - 1 if waiting else Job.attempts))
        if result.rowcount and status == "failed":
            log.error("Dead-letter job id=%s type=%s", lease.id, lease.type)
        return bool(result.rowcount)

    def execute(self, lease):
        from .settlement import Settlement
        try:
            with self.transaction(lease):
                pass
            service = Settlement(self, lease)
            getattr(service, lease.type)(lease.entity_id)
        except LostLease:
            return False
        except Exception as exc:
            return self.finish(lease, exc)
        return self.finish(lease)

    def reconcile(self):
        with self.database.transaction() as db:
            db.execute(update(Job).where(Job.status == "running", Job.lease_until <= now()).values(
                status=case((Job.attempts >= MAX_ATTEMPTS, "failed"), else_="pending"),
                lease_owner=None, lease_until=None, fencing_version=Job.fencing_version + 1,
                next_attempt_at=now(), updated_at=now(), last_error="lease_expired",
            ))
            dead = db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed"))
        if dead:
            log.error("Outbox has %d dead-letter job(s); inspect jobs with status=failed", dead)
        # Keyset pages keep recovery transactions short without starving later
        # entities. enqueue never resurrects dead letters or completed jobs.
        for cls, pk in ((Judgment, Judgment.judgment_id), (JudgmentPair, JudgmentPair.pair_id),
                        (JudgmentBatch, JudgmentBatch.batch_id), (Prepared, Prepared.id)):
            cursor = ""
            while True:
                with self.database.transaction() as db:
                    rows = list(db.scalars(select(cls).where(pk > cursor).order_by(pk).limit(200)))
                    for row in rows:
                        if cls is Judgment:
                            if not row.liability_reserved and not row.enforcement_complete:
                                enqueue(db, "reservation", row.judgment_id)
                            if row.liability_reserved or row.settling or row.enforcement_requested:
                                enqueue(db, "settlement", row.pair_id)
                        elif cls is JudgmentPair:
                            if row.status in ("resolving", "enforcing", "resolved"):
                                enqueue(db, "settlement", row.pair_id)
                        elif cls is JudgmentBatch:
                            unresolved = db.scalar(select(JudgmentPair.pair_id).where(
                                JudgmentPair.batch_id == row.batch_id, JudgmentPair.status != "resolved").limit(1))
                            if not row.anchored and unresolved is None:
                                enqueue(db, "anchor", row.batch_id)
                        elif not row.confirmed:
                            enqueue(db, "prepared", row.id)
                    if rows:
                        cursor = getattr(rows[-1], pk.key)
                if len(rows) < 200:
                    break

    def run_once(self, *, reconcile=True):
        if reconcile:
            self.reconcile()
        jobs = self.claim()
        for job in jobs:
            self.execute(job)
        return len(jobs)


def main():
    from .signing_chain import SigningChain
    from .config import Settings
    from .migrate import check_schema
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Reconcile and process one batch, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    database = Database(settings.database_url)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        check_schema(database.engine)
        chain = SigningChain(settings)
        log.info("Worker signer readiness: %s", chain.check_signers())
        worker = Worker(database, chain, settings)
        next_reconcile = 0
        while not stop.is_set():
            try:
                reconcile = now() >= next_reconcile
                worker.run_once(reconcile=reconcile)
                if reconcile:
                    next_reconcile = now() + 30
            except Exception:
                log.error("Worker pass failed; retrying (details redacted)")
            if args.once:
                break
            stop.wait(1)
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
