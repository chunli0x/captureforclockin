"""Judgment product (RLHF preference judging) API. Run:
uvicorn seeker.api:production_app --factory. Wallet ownership is mandatory."""

import hashlib
import json
import secrets
import functools
from typing import Annotated, NoReturn, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from solders.pubkey import Pubkey
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from .attestation import (
    AttestationError,
    AttestationVerifier,
    LivenessVerification,
    verify_wallet_proof,
)
from .chain import Chain, ChainUnavailable
from .config import Settings
from .outbox import enqueue, judgment_view, faucet_view
from .db import (
    Actor,
    CalibrationAnswer,
    CalibrationPair,
    Challenge,
    Database,
    Judgment,
    Job,
    JudgmentRequest,
    JudgmentBatch,
    JudgmentPair,
    Login,
    Prepared,
    now,
)
from .schemas import (
    Confirm,
    JudgmentSubmit,
    JudgmentAccepted,
    JudgmentOperationView,
    FaucetOperationView,
    LivenessComplete,
    LivenessSession,
    Verify,
    Wallet,
)
from .judge_leveling import level_info as judge_level_info
from . import judgment as judgment_mod
from .liveness import (
    LivenessError,
    LivenessSessionStore,
    SessionAlreadyUsed,
    SessionExpired,
    UnknownSession,
)


@functools.lru_cache(maxsize=1)
def liveness_sessions() -> LivenessSessionStore:
    return LivenessSessionStore()


def fail(status, detail) -> NoReturn:
    raise HTTPException(status, detail)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def lock(db, cls, key):
    # ``populate_existing`` forces the ORM to re-read the row from the freshly
    # acquired SELECT ... FOR UPDATE instead of returning an identity-map cache
    # hit, so a handler that loaded a row before taking the lock observes fresh
    # state and cannot pass an immutable-state guard on stale data.
    row = db.scalar(
        select(cls)
        .where(getattr(cls, next(iter(cls.__table__.primary_key.columns)).name) == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        fail(404, "Resource not found")
    return row


def insert_once(db, cls, values):
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(insert(cls).values(**values).on_conflict_do_nothing())


def create_app(settings: Settings, *, chain=None, attestation_verifier: AttestationVerifier | None = None):
    app = FastAPI(title="Capture judgment product", version="0.1.0")
    attestation_verifier = attestation_verifier or AttestationVerifier()
    database = Database(settings.database_url)
    from .migrate import check_schema
    from .rate_limit import RequestLimitsMiddleware

    try:
        check_schema(database.engine)
    except Exception:
        database.engine.dispose()
        raise
    app.add_middleware(RequestLimitsMiddleware, database=database, settings=settings)
    chain = chain or Chain(settings)
    app.state.database, app.state.chain = database, chain

    @app.exception_handler(ChainUnavailable)
    async def unavailable(_, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        if request.url.path.startswith("/media/"):
            return JSONResponse(status_code=422, content={"detail": "Invalid video request"})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(IntegrityError)
    async def conflict(_, exc):
        return JSONResponse(
            status_code=409,
            content={"detail": "Concurrent or duplicate operation; reload and retry"},
        )

    def current(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            fail(401, "Wallet sign-in required")
        with database.transaction() as db:
            login = db.get(Login, digest(authorization[7:].encode()))
            if not login or login.expires <= now():
                fail(401, "Session expired or invalid")
            return db.get(Actor, login.wallet)

    Auth = Annotated[Actor, Depends(current)]
    from .media_routes import install_media_routes, video_pair_view, video_ready, require_viewing_opportunity
    from .db import MediaAccess
    install_media_routes(app, settings, database, current)

    def liveness_fresh(actor):
        return LivenessVerification.from_actor(actor).is_fresh(
            now=now(), demo_allowed=settings.liveness_demo,
        )

    def liveness_status(actor):
        fresh = liveness_fresh(actor)
        return {
            "liveness_completed": fresh or not settings.liveness_required,
            "liveness_fresh": fresh,
            "liveness_required": settings.liveness_required,
            "liveness_demo": settings.liveness_demo,
            "liveness_verified_at": actor.liveness_verified_at,
            "liveness_method": actor.liveness_method,
            "liveness_expires_at": actor.liveness_expires_at,
            "liveness_device_integrity": actor.liveness_device_integrity or "none",
        }

    @app.get("/health")
    def health():
        """Real readiness probe: database connect, schema applied, chain
        reachable. 200 when all pass, 503 otherwise."""
        status = 200
        checks = {}

        try:
            with database.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "down"
            status = 503

        try:
            check_schema(database.engine)
            checks["schema"] = "applied"
        except Exception:
            checks["schema"] = "unavailable"
            status = 503

        try:
            chain.check_environment()
            checks["chain"] = "reachable"
        except Exception:
            checks["chain"] = "unreachable"
            status = 503

        return JSONResponse(
            status_code=status,
            content={
                "service": "seeker",
                "network": settings.cluster,
                "production_ready": status == 200,
                "checks": checks,
                # Judgment product honesty (test tokens + provenance anchors).
                "test_tokens": True,
                "liveness_demo": settings.liveness_demo,
                "liveness_required": settings.liveness_required,
                "dataset_bias": "majority-preference",
                "skr_mint": settings.skr_mint,
                "program_id": settings.program_id,
                "treasury": settings.treasury_pubkey,
                "slash_authority": settings.verifier_pubkey,
            },
        )

    @app.post("/auth/challenge")
    def challenge(body: Wallet):
        nonce, expires = secrets.token_hex(32), now() + 120
        message = f"{settings.domain} requests Capture judgment test-pilot sign-in.\nWallet: {body.wallet}\nNetwork: {settings.cluster}\nNonce: {nonce}\nExpires: {expires}\nThis does not authorize any token transfer."
        with database.transaction() as db:
            db.add(
                Challenge(
                    id=nonce, wallet=body.wallet, message=message, expires=expires
                )
            )
        return {"challenge_id": nonce, "message": message, "expires": expires}

    @app.post("/auth/verify")
    def verify(body: Verify):
        with database.transaction() as db:
            c = lock(db, Challenge, body.challenge_id)
            if c.used or c.expires <= now():
                fail(401, "Challenge expired or already consumed")
            try:
                verify_wallet_proof(wallet=c.wallet, message=c.message.encode(),
                                    signature=body.signature, expires_at=c.expires, now=now())
            except AttestationError as exc:
                fail(401, str(exc))
            c.used = True
            insert_once(
                db, Actor,
                {"wallet": c.wallet, "over_18": body.over_18},
            )
            token = secrets.token_urlsafe(32)
            expires = now() + settings.session_seconds
            db.add(
                Login(digest=digest(token.encode()), wallet=c.wallet, expires=expires)
            )
        return {"token": token, "wallet": c.wallet, "expires": expires}

    @app.post("/auth/logout")
    def logout(actor: Auth, authorization: Annotated[str | None, Header()] = None):
        with database.transaction() as db:
            login = db.get(Login, digest(authorization[7:].encode()))
            if login:
                db.delete(login)
        return {"signed_out": True}

    @app.get("/me")
    def me(actor: Auth):
        with database.transaction() as db:
            level = judge_level_info(actor.agreement_rate)
        return {
            "wallet": actor.wallet,
            "over_18": actor.over_18,
            **liveness_status(actor),
            "bond_test_skr_units": settings.min_bond,
            "bonded": actor.bonded,
            "skr_funded": actor.skr_funded,
            "faucet_mints": actor.faucet_count,
            "faucet_max": settings.faucet_max_mints,
            "calibrated": actor.calibrated,
            "staked_skr": actor.staked_skr,
            "warnings": actor.warnings,
            **level,
        }

    @app.post("/liveness/session")
    def liveness_session(body: LivenessSession, actor: Auth):
        Pubkey.from_string(body.contributor)
        if body.contributor != actor.wallet:
            fail(403, "Liveness contributor must match the authenticated wallet")
        store = liveness_sessions()
        session = store.issue(actor.wallet, body.task_id)
        return {
            "session_id": session.session_id,
            "challenge": session.challenge.value,
            "nonce": session.nonce.hex(),
            "expires_at": session.expires_at,
            "ttl_seconds": store.ttl_seconds,
        }

    @app.post("/liveness/complete")
    def liveness_complete(body: LivenessComplete, actor: Auth):
        if body.contributor != actor.wallet:
            fail(403, "Liveness contributor must match the authenticated wallet")
        try:
            result_digest = bytes.fromhex(body.result_digest)
        except ValueError:
            fail(400, "result_digest must be hex")
        store = liveness_sessions()
        try:
            session = store.get(actor.wallet, body.session_id)
            digest_ = store.complete(actor.wallet, body.session_id, result_digest,
                                     liveness_demo=settings.liveness_demo)
            verification = attestation_verifier.verify_liveness(
                wallet=actor.wallet, proof_hash=digest_, result_digest=result_digest,
                device_token=body.device_integrity_token, now=now(),
                ttl_seconds=settings.liveness_verification_seconds,
                demo_allowed=settings.liveness_demo,
            )
        except UnknownSession as exc:
            fail(404, str(exc))
        except SessionExpired as exc:
            fail(410, str(exc))
        except SessionAlreadyUsed as exc:
            fail(409, str(exc))
        except LivenessError as exc:
            fail(400, str(exc))
        with database.transaction() as db:
            worker = lock(db, Actor, actor.wallet)
            if not verification.is_fresh(now=now(), demo_allowed=settings.liveness_demo):
                fail(410, "Liveness verification expired; request a new session")
            worker.liveness_verified_at = verification.verified_at
            worker.liveness_method = verification.method
            worker.liveness_expires_at = verification.expires_at
            worker.liveness_device_integrity = verification.device_integrity
        return {
            "liveness_proof_hash": digest_.hex(),
            "task_id": session.task_id,
            **liveness_status(worker),
            "attests": (
                "demo bypass; no motion verified" if verification.method == "demo" else
                "client reports a completed on-device motion challenge"
            ),
        }

    # ---- Bond (stake) ----

    @app.post("/bond/faucet")
    def bond_faucet(actor: Auth):
        """Reserve quota and enqueue atomically. Only the worker may mint."""
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            # A retry while this wallet is waiting returns the same operation.
            pending = db.scalar(select(Job).where(
                Job.type == "faucet", Job.entity_id == actor.wallet,
                Job.status.in_(("accepted", "pending", "running")),
            ))
            if pending is not None:
                return JSONResponse(status_code=202, content=faucet_view(pending, actor, settings))
            if actor.faucet_count >= settings.faucet_max_mints:
                return {
                    "funded": actor.skr_funded, "already": True,
                    "mints": actor.faucet_count, "max": settings.faucet_max_mints,
                }
            actor.faucet_count += 1
            job = enqueue(db, "faucet", actor.wallet, version=actor.faucet_count)
            response = faucet_view(job, actor, settings)
        return JSONResponse(status_code=202, content=response)

    @app.post("/bond/deposit")
    def bond_deposit(actor: Auth):
        """Build + return the real ``deposit_bond`` prepare transaction. The
        bond is marked ``bonded`` only when the client confirms the exact
        message via /transactions/{id}/confirm."""
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if not actor.skr_funded:
                fail(409, "Get test $SKR first")
        return prepare("deposit", actor.wallet, actor)

    @app.post("/bond/withdraw")
    def bond_withdraw(actor: Auth):
        """Build + return the real ``withdraw_bond`` prepare transaction.
        ``bonded`` is cleared only on confirm."""
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if not actor.bonded:
                fail(403, "Bond deposit required")
            # Finding A (blockchain-dev review): the on-chain program allows
            # withdraw after the first warn; freeze withdrawal once warned.
            if actor.warnings >= 1:
                fail(403, "Bond has an active warning; withdrawal is frozen")
        return prepare("withdraw", actor.wallet, actor)

    @app.post("/bond/{action}/prepare")
    def prepare_bond(action: str, actor: Auth):
        if action not in {"deposit", "withdraw"}:
            fail(404, "Unknown action")
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if action == "deposit":
                if not actor.skr_funded:
                    fail(409, "Get test $SKR first")
            else:
                if not actor.bonded:
                    fail(403, "Bond deposit required")
                if actor.warnings >= 1:
                    fail(403, "Bond has an active warning; withdrawal is frozen")
        return prepare(action, actor.wallet, actor)

    # ---- Judgment product (RLHF preference judging) ----

    def calibration_progress(db, wallet):
        answered = db.scalar(
            select(func.count())
            .select_from(CalibrationAnswer)
            .where(CalibrationAnswer.wallet == wallet)
        )
        correct = db.scalar(
            select(func.count())
            .select_from(CalibrationAnswer)
            .where(CalibrationAnswer.wallet == wallet, CalibrationAnswer.correct.is_(True))
        )
        return answered, correct

    def _pair_view(p):
        return {
            "pair_id": p.pair_id,
            "task_type": p.task_type,
            "media_type": p.media_type,
            "output_a_url": p.output_a_url,
            "output_b_url": p.output_b_url,
            "rubric": p.rubric,
        }

    def _validate_against_task(task_type, body):
        if task_type == "rating":
            if body.choice == "cant_tell":
                return "cant_tell", None
            if body.rating is None:
                fail(422, "Rating task requires a rating 1-5 (or 'cant_tell')")
            return "rating", body.rating
        if body.rating is not None:
            fail(422, "Preference task requires a choice, not a rating")
        if body.choice is None:
            fail(422, "Preference task requires a choice (a/b/tie/cant_tell)")
        return body.choice, None

    def _calibration_status(actor, answered, correct):
        # Mid-gate we do NOT reveal the running correct count: a judge who can
        # see per-answer correctness can binary-search the known answer of each
        # calibration pair (finding #6).
        in_progress = (not actor.calibrated) and 0 < answered < settings.calibration_n
        return {
            "kind": "calibration",
            "status": (
                "calibrated"
                if actor.calibrated
                else ("retry" if answered == 0 else "in_progress")
            ),
            "calibrated": actor.calibrated,
            "calibration_progress": {
                "completed": answered,
                "required": settings.calibration_n,
                "correct": None if in_progress else correct,
            },
        }

    @app.get("/judge/next")
    def judge_next(actor: Auth, exclude_pair_id: Annotated[list[str] | None, Query()] = None,
                   modality: Literal["image", "video", "all"] = "image"):
        # Unpaid, request-scoped exclusions: no vote, liability, or payout is created.
        # Clients retain skipped IDs across subsequent fetches in this session.
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if settings.liveness_required and not liveness_fresh(actor):
                return {
                    "kind": "none",
                    "calibrated": True,
                    "reason": "prove you're human before judging",
                }
            if not actor.bonded:
                return {"kind": "none", "calibrated": True, "reason": "deposit the SKR bond to judge"}
            done = select(Judgment.pair_id).where(Judgment.wallet == actor.wallet)
            skipped = select(MediaAccess.pair_id).where(MediaAccess.wallet == actor.wallet, MediaAccess.skipped.is_(True))
            candidates = db.scalars(
                select(JudgmentPair)
                .where(
                    JudgmentPair.status.in_(["open", "collecting", "rerouted"]),
                    JudgmentPair.pair_id.not_in(done),
                    JudgmentPair.pair_id.not_in(skipped),
                    JudgmentPair.media_type.in_(["image", "video"] if modality == "all" else [modality]),
                    JudgmentPair.pair_id.not_in(exclude_pair_id or []),
                )
                .order_by(JudgmentPair.created_at)
            )
            pair = next((p for p in candidates if p.media_type == "image" or video_ready(db, p)), None)
            if pair is None:
                return {"kind": "none", "calibrated": True}
            view = (video_pair_view(db, pair, actor.wallet, settings) if pair.media_type == "video"
                    else _pair_view(pair))
            view.update({"kind": "judgment", "calibrated": True, "calibration_remaining": 0})
            return view

    @app.post("/judge/{pair_id}", responses={202: {"model": JudgmentAccepted}})
    def judge_submit(pair_id: str, body: JudgmentSubmit, actor: Auth,
                     idempotency_key: Annotated[str | None, Header(min_length=1, max_length=256)] = None):
        with database.transaction() as db:
            is_calibration = db.get(CalibrationPair, pair_id) is not None
        if is_calibration:
            with database.transaction() as db:
                return _submit_calibration(db, pair_id, body, actor.wallet)
        return _submit_judgment(pair_id, body, actor.wallet, idempotency_key)

    def _submit_calibration(db, pair_id, body, wallet):
        actor = lock(db, Actor, wallet)
        cp = lock(db, CalibrationPair, pair_id)
        existing = db.scalar(
            select(CalibrationAnswer).where(
                CalibrationAnswer.pair_id == pair_id, CalibrationAnswer.wallet == wallet
            )
        )
        if existing:
            answered, correct = calibration_progress(db, wallet)
            return _calibration_status(actor, answered, correct)
        choice, rating = _validate_against_task(cp.task_type, body)
        correct = judgment_mod.calibration_correct(cp.task_type, cp.known_choice, choice, rating)
        db.add(
            CalibrationAnswer(
                pair_id=pair_id, wallet=wallet, choice=choice or "rating",
                rating=rating, correct=correct,
            )
        )
        db.flush()
        answered, ncorrect = calibration_progress(db, wallet)
        if answered >= settings.calibration_n:
            threshold = max(1, round(settings.calibration_n * 0.8))
            if ncorrect >= threshold:
                actor.calibrated = True
            else:
                db.execute(delete(CalibrationAnswer).where(CalibrationAnswer.wallet == wallet))
                answered, ncorrect = 0, 0
        return _calibration_status(actor, answered, ncorrect)

    def _submit_judgment(pair_id, body, wallet, idempotency_key):
        body_hash = digest(json.dumps({"pair_id": pair_id, **body.model_dump()},
                                      sort_keys=True, separators=(",", ":")).encode())
        key = digest(idempotency_key.encode()) if idempotency_key else None
        with database.transaction() as db:
            pair = lock(db, JudgmentPair, pair_id)
            actor = lock(db, Actor, wallet)
            prior = db.get(JudgmentRequest, (wallet, key)) if key else None
            if prior:
                if prior.body_hash != body_hash:
                    fail(409, "Idempotency key was used with a different body")
                j = db.get(Judgment, prior.judgment_id)
            else:
                choice, rating = _validate_against_task(pair.task_type, body)
                j = db.scalar(select(Judgment).where(
                    Judgment.pair_id == pair_id, Judgment.wallet == wallet))
                if j and (j.choice, j.rating) != (choice, rating):
                    fail(409, "You already judged this pair")
                if j is None:
                    if settings.liveness_required and not liveness_fresh(actor):
                        fail(403, "Prove you're human before judging")
                    if not actor.bonded:
                        fail(403, "Deposit the SKR bond to judge")
                    if pair.status not in ("open", "collecting", "rerouted"):
                        fail(409, "Pair no longer accepts judgments")
                    if pair.media_type == "video":
                        require_viewing_opportunity(db, pair, wallet)
                    j = Judgment(pair_id=pair_id, wallet=wallet, choice=choice, rating=rating)
                    db.add(j)
                    db.flush()
                if key:
                    db.add(JudgmentRequest(wallet=wallet, key=key, body_hash=body_hash,
                                           judgment_id=j.judgment_id))
            # This INSERT and the immutable decision share ONE commit. There
            # is no post-commit enqueue or in-process background callback.
            operation = enqueue(db, "reservation", j.judgment_id)
            response = {"status": "accepted", "judgment_id": j.judgment_id,
                        "operation_id": operation.id}
        return JSONResponse(status_code=202, content=response)

    @app.get("/judgments/{judgment_id}", response_model=JudgmentOperationView)
    def get_judgment(judgment_id: str, actor: Auth):
        with database.transaction() as db:
            j = db.get(Judgment, judgment_id)
            if not j or j.wallet != actor.wallet:
                fail(404, "Judgment not found")
            return judgment_view(db, j)

    @app.get("/operations/{operation_id}", response_model=JudgmentOperationView | FaucetOperationView)
    def get_operation(operation_id: str, actor: Auth):
        with database.transaction() as db:
            job = db.get(Job, operation_id)
            if job and job.type == "faucet" and job.entity_id == actor.wallet:
                return faucet_view(job, db.get(Actor, actor.wallet), settings)
            j = db.get(Judgment, job.entity_id) if job and job.type == "reservation" else None
            if not j or j.wallet != actor.wallet:
                fail(404, "Operation not found")
            return judgment_view(db, j)

    @app.get("/judge/stats")
    def judge_stats(actor: Auth):
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            answered, correct = calibration_progress(db, actor.wallet)
            level = judge_level_info(actor.agreement_rate)
            in_progress = (not actor.calibrated) and 0 < answered < settings.calibration_n
            earned = db.scalar(
                select(func.coalesce(func.sum(Judgment.earned_skr), 0)).where(
                    Judgment.wallet == actor.wallet
                )
            )
            settled = db.scalar(
                select(func.coalesce(func.sum(Judgment.earned_skr), 0)).where(
                    Judgment.wallet == actor.wallet,
                    Judgment.payout_signature.is_not(None),
                )
            )
            return {
                "calibrated": actor.calibrated,
                "calibration_progress": {
                    "completed": answered,
                    "required": settings.calibration_n,
                    "correct": None if in_progress else correct,
                },
                "agreement_rate": actor.agreement_rate,
                "judged_count": actor.judged_count,
                "level": level["level"],
                "level_name": level["level_name"],
                "rate_multiplier": level["rate_multiplier"],
                "base_skr": settings.judge_base_skr,
                "flat_skr": settings.judge_flat_skr,
                "staked_skr": actor.staked_skr,
                "warnings": actor.warnings,
                "earned_skr": earned,
                "settled_skr": settled,
            }

    @app.get("/batch/{batch_id}/dataset")
    def batch_dataset(batch_id: str, actor: Auth):
        with database.transaction() as db:
            batch = db.get(JudgmentBatch, batch_id)
            if batch is None:
                fail(404, "Batch not found")
            pairs = db.scalars(
                select(JudgmentPair)
                .where(JudgmentPair.batch_id == batch_id, JudgmentPair.status == "resolved")
                .order_by(JudgmentPair.pair_id)
            ).all()
            entries = [
                {
                    "pair_id": p.pair_id,
                    "output_a_url": p.output_a_url,
                    "output_b_url": p.output_b_url,
                    "preference": p.preference,
                    "agreement": p.agreement,
                    "confidence": p.confidence,
                    "media_type": p.media_type,
                    "task_type": p.task_type,
                    "rubric": p.rubric,
                }
                for p in pairs
            ]
            return {
                "batch_id": batch_id,
                "merkle_root": batch.merkle_root,
                "anchored": batch.anchored,
                "entries": entries,
            }

    # ---- Prepared transactions (bond deposit/withdraw) ----

    def prepare(action, target, actor):
        with database.transaction() as db:
            old = db.scalars(select(Prepared).where(
                Prepared.wallet == actor.wallet, Prepared.action == action,
                Prepared.target == target, Prepared.confirmed.is_(False),
            ).order_by(Prepared.last_valid_height.desc())).first()
        if old and old.last_valid_height >= chain.height():
            return tx_view(old)
        result = chain.prepare(action, actor.wallet)
        with database.transaction() as db:
            p = Prepared(wallet=actor.wallet, action=action, target=target, **result)
            db.add(p)
            db.flush()
            enqueue(db, "prepared", p.id)
            return tx_view(p)

    def tx_view(p):
        return {
            "id": p.id,
            "action": p.action,
            "target": p.target,
            "transaction_base64": p.transaction,
            "last_valid_block_height": p.last_valid_height,
            "confirmed": p.confirmed,
            "signature": p.signature,
        }

    @app.get("/transactions")
    def transactions(actor: Auth):
        with database.transaction() as db:
            rows = db.scalars(
                select(Prepared)
                .where(Prepared.wallet == actor.wallet)
                .order_by(Prepared.last_valid_height.desc())
                .limit(100)
            ).all()
            return [tx_view(p) for p in rows]

    def record_confirmation(pid, actor, signature=None):
        # Phase 1: read + authorize under a short transaction, capture the
        # inputs the RPC calls need, then release every row lock. Network work
        # (signature walk + verification) must never hold a DB lock.
        with database.transaction() as db:
            lock(db, Actor, actor.wallet)
            p = db.get(Prepared, pid)
            if not p or p.wallet != actor.wallet:
                fail(404, "Prepared transaction not found")
            p = lock(db, Prepared, pid)
            if p.confirmed:
                if signature and signature != p.signature:
                    fail(409, "Already confirmed with another signature")
                return tx_view(p)
            prepared = p

        # Phase 2: network I/O with no DB locks held.
        resolved = signature or chain.find_signature(prepared)
        chain.confirm(prepared, resolved)
        # A fresh post-slash deposit resets warnings; a top-up does not.
        warnings = chain.bond_warnings(prepared.wallet) if prepared.action == "deposit" else None

        # Phase 3: re-acquire the lock and write atomically, tolerating a
        # concurrent confirmation that landed between phases.
        with database.transaction() as db:
            lock(db, Actor, actor.wallet)
            p = lock(db, Prepared, pid)
            if p.confirmed:
                if signature and signature != p.signature:
                    fail(409, "Already confirmed with another signature")
                return tx_view(p)
            p.signature, p.confirmed = resolved, True
            if p.action == "deposit":
                worker = lock(db, Actor, p.wallet)
                worker.bonded = True
                worker.warnings = min(warnings, 2)
                worker.staked_skr = settings.min_bond
            elif p.action == "withdraw":
                worker = lock(db, Actor, p.wallet)
                worker.bonded = False
                worker.staked_skr = 0
            return tx_view(p)

    @app.post("/transactions/{pid}/confirm")
    def confirm(pid: str, body: Confirm, actor: Auth):
        return record_confirmation(pid, actor, body.signature)

    @app.post("/transactions/{pid}/reconcile")
    def reconcile(pid: str, actor: Auth):
        return record_confirmation(pid, actor)

    return app


def production_app():
    return create_app(Settings())
