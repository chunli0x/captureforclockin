"""Operator commissioning and authenticated, wallet-bound video delivery."""
import hashlib
import json
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from .db import (Actor, IngestJob, Judgment, JudgmentBatch, JudgmentPair, MediaAccess,
                 MediaAsset, MediaVariant, now, uid)
from .media import MediaError, sign_delivery, verify_delivery
from .media_ingest import asset_path, write_private
from .schemas import (ApprovedVariant, IngestView, MediaApproval, VideoCommission,
                      VideoManifest, VideoPairView)


def deny(status=404):
    raise HTTPException(status, "Video unavailable")


def approved(db, asset_id):
    asset = db.get(MediaAsset, asset_id) if asset_id else None
    if asset is None or asset.status != "approved" or not asset.approved_by or not asset.approved_at:
        deny()
    variant = db.scalar(select(MediaVariant).where(MediaVariant.asset_id == asset_id,
                                                   MediaVariant.version == asset.version))
    if variant is None or variant.sha256 != asset.approved_hash:
        deny()
    return asset, variant


def video_ready(db, pair):
    try:
        a, va = approved(db, pair.asset_a_id)
        b, vb = approved(db, pair.asset_b_id)
        return (va.duration_ms, va.width, va.height, va.fps) == (vb.duration_ms, vb.width, vb.height, vb.fps)
    except HTTPException:
        return False


def video_pair_view(db, pair, wallet, settings):
    if not video_ready(db, pair):
        deny()
    if not settings.media_signing_secret:
        deny(503)
    expires = now() + settings.media_url_seconds

    def manifest(aid):
        asset, v = approved(db, aid)
        token = sign_delivery(dict(wallet=wallet, pair=pair.pair_id, asset=aid, variant=v.variant_id,
                                   version=v.version, sha256=v.sha256, revision=asset.revision, expires=expires),
                              settings.media_signing_secret)
        url = f"/media/deliver/{v.variant_id}?token={token}"
        return VideoManifest(asset_id=aid, version=v.version, sha256=v.sha256,
            duration_ms=v.duration_ms, width=v.width, height=v.height, expires_at=expires,
            approved_variants=[ApprovedVariant(variant_id=v.variant_id, version=v.version, sha256=v.sha256,
                duration_ms=v.duration_ms, width=v.width, height=v.height, fps=v.fps, url=url, expires_at=expires)])
    a, b = manifest(pair.asset_a_id), manifest(pair.asset_b_id)
    access = db.get(MediaAccess, (wallet, pair.pair_id))
    if access is None:
        db.add(MediaAccess(wallet=wallet, pair_id=pair.pair_id))
    elif access.skipped:
        deny()
    return VideoPairView(pair_id=pair.pair_id, output_a_url=a.approved_variants[0].url,
                         output_b_url=b.approved_variants[0].url, media_a=a, media_b=b).model_dump()


def require_viewing_opportunity(db, pair, wallet):
    access = db.get(MediaAccess, (wallet, pair.pair_id))
    if not video_ready(db, pair) or access is None or access.skipped:
        deny(409)
    if access.served_a_at is None or access.served_b_at is None or now() < max(access.served_a_at, access.served_b_at) + 4:
        raise HTTPException(409, "Load and view both videos before submitting")


def install_media_routes(app, settings, database, current):
    Auth = Annotated[Actor, Depends(current)]

    def operator(actor):
        if actor.wallet not in settings.media_operators:
            deny(403)

    def reviewer(actor):
        if actor.wallet not in settings.media_reviewers:
            deny(403)

    @app.post("/media/ingest", response_model=IngestView, status_code=202)
    async def ingest(request: Request, actor: Auth):
        operator(actor)
        if request.headers.get("content-type") != "video/mp4":
            deny(415)
        data = await request.body()
        if len(data) < 12 or len(data) > settings.media_max_bytes or data[4:8] != b"ftyp":
            deny(422)
        aid, jid = uid(), uid()
        path = asset_path(settings, "quarantine", aid)
        write_private(path, data)
        try:
            with database.transaction() as db:
                db.add(MediaAsset(asset_id=aid))
                db.flush()
                db.add(IngestJob(job_id=jid, asset_id=aid))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return IngestView(asset_id=aid, job_id=jid, status="pending")

    @app.get("/media/ingest/{asset_id}", response_model=IngestView)
    def status(asset_id: str, actor: Auth):
        if actor.wallet not in settings.media_operators + settings.media_reviewers:
            deny(403)
        with database.transaction() as db:
            job = db.scalar(select(IngestJob).where(IngestJob.asset_id == asset_id))
            if not job:
                deny()
            v = db.scalar(select(MediaVariant).where(MediaVariant.asset_id == asset_id))
            return IngestView(asset_id=asset_id, job_id=job.job_id, status=job.status,
                              sha256=v.sha256 if v else None, error_code=job.error_code)

    @app.post("/media/assets/{asset_id}/approve", response_model=IngestView)
    def approve(asset_id: str, body: MediaApproval, actor: Auth):
        reviewer(actor)
        with database.transaction() as db:
            asset = db.scalar(select(MediaAsset).where(MediaAsset.asset_id == asset_id).with_for_update())
            if not asset or asset.status != "review":
                deny(409)
            v = db.scalar(select(MediaVariant).where(MediaVariant.asset_id == asset_id))
            if v is None or body.sha256 != v.sha256:
                deny(409)
            source = asset_path(settings, "review", v.variant_id)
            try:
                data = source.read_bytes()
            except OSError:
                deny(409)
            if hashlib.sha256(data).hexdigest() != body.sha256:
                deny(409)
            destination = asset_path(settings, "approved", v.variant_id)
            # Immutable artifact: retry after a crash accepts only identical bytes.
            if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != v.sha256:
                deny(409)
            if not destination.exists():
                write_private(destination, data)
                destination.chmod(0o400)
            # Persist an immutable neutral manifest; expiring URLs are a separate
            # wallet-specific delivery envelope, never stored in this artifact.
            manifest_path = destination.with_suffix(".json")
            manifest = json.dumps({
                "modality": "video", "asset_id": asset_id, "version": v.version,
                "sha256": v.sha256, "duration_ms": v.duration_ms,
                "width": v.width, "height": v.height, "fps": v.fps,
                "approved_variants": [{"variant_id": v.variant_id, "sha256": v.sha256,
                                       "content_type": "video/mp4"}],
            }, sort_keys=True, separators=(",", ":")).encode()
            if manifest_path.exists() and manifest_path.read_bytes() != manifest:
                deny(409)
            if not manifest_path.exists():
                write_private(manifest_path, manifest)
                manifest_path.chmod(0o400)
            asset.status, asset.approved_by, asset.approved_at = "approved", actor.wallet, now()
            asset.approved_hash = v.sha256
            job = db.scalar(select(IngestJob).where(IngestJob.asset_id == asset_id))
            job.status = "approved"
            return IngestView(asset_id=asset_id, job_id=job.job_id, status="approved", sha256=v.sha256)

    @app.post("/media/assets/{asset_id}/revoke")
    def revoke(asset_id: str, actor: Auth):
        reviewer(actor)
        with database.transaction() as db:
            asset = db.scalar(select(MediaAsset).where(MediaAsset.asset_id == asset_id).with_for_update())
            if asset is None:
                deny()
            asset.status, asset.revision = "revoked", asset.revision + 1
            job = db.scalar(select(IngestJob).where(IngestJob.asset_id == asset_id))
            job.status = "revoked"
        return {"status": "revoked"}

    @app.post("/media/pairs", response_model=VideoPairView)
    def commission(body: VideoCommission, actor: Auth):
        operator(actor)
        if body.asset_a_id == body.asset_b_id:
            deny(422)
        with database.transaction() as db:
            approved(db, body.asset_a_id)
            approved(db, body.asset_b_id)
            batch = JudgmentBatch(batch_id=uid())
            db.add(batch)
            db.flush()
            pair = JudgmentPair(pair_id=uid(), batch_id=batch.batch_id, media_type="video", task_type="preference",
                                asset_a_id=body.asset_a_id, asset_b_id=body.asset_b_id, k=settings.judge_k,
                                rubric="Compare robot vision video outputs.")
            db.add(pair)
            db.flush()
            return video_pair_view(db, pair, actor.wallet, settings)

    @app.api_route("/media/deliver/{variant_id}", methods=["GET", "HEAD"])
    def deliver(variant_id: str, token: str, request: Request, actor: Auth):
        try:
            payload = verify_delivery(token, settings.media_signing_secret, actor.wallet)
        except MediaError:
            deny(403)
        with database.transaction() as db:
            asset, v = approved(db, payload.get("asset"))
            pair = db.get(JudgmentPair, payload.get("pair"))
            if (v.variant_id != variant_id or payload.get("variant") != variant_id or
                    payload.get("version") != v.version or payload.get("sha256") != v.sha256 or
                    payload.get("revision") != asset.revision or pair is None or
                    asset.asset_id not in (pair.asset_a_id, pair.asset_b_id) or not video_ready(db, pair)):
                deny(403)
            access = db.get(MediaAccess, (actor.wallet, pair.pair_id))
            if access is None or access.skipped:
                deny(403)
            path = asset_path(settings, "approved", variant_id)
            try:
                # Local slice: verify immutability on every request, including HEAD/Range.
                if hashlib.sha256(path.read_bytes()).hexdigest() != v.sha256:
                    deny()
            except OSError:
                deny()
            if request.method == "GET":
                if asset.asset_id == pair.asset_a_id and access.served_a_at is None:
                    access.served_a_at = now()
                if asset.asset_id == pair.asset_b_id and access.served_b_at is None:
                    access.served_b_at = now()
        # FileResponse implements RFC byte ranges (206/416). Query tokens stay in
        # every URL/cache key. No shared/CDN caching is enabled for this slice.
        return FileResponse(path, media_type="video/mp4", headers={
            "Cache-Control": "private, no-store", "Vary": "Authorization", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="clip.mp4"', "Referrer-Policy": "no-referrer",
        })

    @app.post("/judge/{pair_id}/media-skip")
    def media_skip(pair_id: str, actor: Auth):
        with database.transaction() as db:
            # Serialize skip vs acceptance in the same pair->wallet lock order.
            pair = db.scalar(select(JudgmentPair).where(JudgmentPair.pair_id == pair_id).with_for_update())
            db.scalar(select(Actor).where(Actor.wallet == actor.wallet).with_for_update())
            if not pair or pair.media_type != "video":
                deny()
            if db.scalar(select(Judgment.judgment_id).where(
                    Judgment.pair_id == pair_id, Judgment.wallet == actor.wallet)):
                deny(409)
            access = db.get(MediaAccess, (actor.wallet, pair_id))
            if access is None:
                deny(403)
            access.skipped = True
        # Deliberately creates no Judgment, reservation, settlement, or flat pay.
        return {"status": "skipped", "paid": False}
