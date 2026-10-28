"""Private robot vision ingest worker: python -m seeker.media_worker [--once]."""
import argparse
import json
import signal
import threading

from sqlalchemy import select, text

from .db import Database, IngestJob, MediaAsset, MediaVariant, now, uid
from .media import MediaError, inspect_video, perceptual_duplicate
from .media_ingest import asset_path, bucket, transcode, write_private


class MediaWorker:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def run_once(self):
        with self.database.transaction() as db:
            stale = db.scalars(select(IngestJob).where(
                IngestJob.status == "running", IngestJob.lease_until <= now()).with_for_update()).all()
            for job in stale:
                job.status = "failed" if job.attempts >= 3 else "pending"
                job.lease_token = None
                if job.status == "failed":
                    job.error_code = "processing_failed"
                    db.get(MediaAsset, job.asset_id).status = "failed"
            job = db.scalar(select(IngestJob).where(IngestJob.status == "pending")
                            .order_by(IngestJob.created_at).limit(1).with_for_update(skip_locked=True))
            if job is None:
                return False
            job.status, job.lease_token, job.lease_until = "running", uid(), now() + 600
            job.attempts += 1
            jid, aid, lease = job.job_id, job.asset_id, job.lease_token
        candidate = None
        try:
            data, attributes = transcode(asset_path(self.settings, "quarantine", aid), bucket(self.settings, "work"))
            vid = uid()
            candidate = asset_path(self.settings, "review", vid)
            write_private(candidate, data)
            inspect_video(candidate, self.settings)
            with self.database.transaction() as db:
                # Serialize duplicate decisions across workers on PostgreSQL;
                # SQLite already serializes write transactions.
                if db.bind.dialect.name == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(1936024940)"))
                row = db.scalar(select(IngestJob).where(IngestJob.job_id == jid).with_for_update())
                if row.status != "running" or row.lease_token != lease or row.lease_until <= now():
                    raise MediaError()
                hashes = json.loads(attributes["dhashes"])
                if any(perceptual_duplicate(hashes, json.loads(v)) for v in db.scalars(select(MediaVariant.dhashes))):
                    raise MediaError("duplicate")
                db.add(MediaVariant(variant_id=vid, asset_id=aid, version=1, **attributes))
                db.get(MediaAsset, aid).status = "review"
                row.status, row.lease_token, row.lease_until = "review", None, None
            candidate = None  # This private derivative now belongs to the review queue.
        except Exception as exc:
            code = exc.code if isinstance(exc, MediaError) else "processing_failed"
            with self.database.transaction() as db:
                row = db.scalar(select(IngestJob).where(IngestJob.job_id == jid).with_for_update())
                if row.status == "running" and row.lease_token == lease and row.lease_until > now():
                    row.status, row.error_code, row.lease_token = "failed", code, None
                    db.get(MediaAsset, aid).status = "failed"
        finally:
            if candidate:
                candidate.unlink(missing_ok=True)
        return True


def main():
    from .config import Settings
    from .migrate import check_schema
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--review-list", action="store_true", help="List private local review derivatives; serves no media")
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_url)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        check_schema(database.engine)
        if args.review_list:
            with database.transaction() as db:
                for asset, variant in db.execute(select(MediaAsset, MediaVariant).join(
                        MediaVariant, MediaAsset.asset_id == MediaVariant.asset_id).where(MediaAsset.status == "review")):
                    print(json.dumps({"asset_id": asset.asset_id, "sha256": variant.sha256,
                                      "local_path": str(asset_path(settings, "review", variant.variant_id))}))
            return
        worker = MediaWorker(database, settings)
        while not stop.is_set():
            worker.run_once()
            if args.once:
                break
            stop.wait(1)
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
