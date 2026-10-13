"""Judgment product (RLHF preference judging) API. Run:
uvicorn seeker.api:production_app --factory. Wallet ownership is mandatory."""

import hashlib
import secrets
import functools
from typing import Annotated, NoReturn

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from solders.pubkey import Pubkey
from solders.signature import Signature
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from .chain import Chain, ChainUnavailable
from .config import Settings
from .db import (
    Actor,
    CalibrationAnswer,
    CalibrationPair,
    Challenge,
    Database,
    Judgment,
    JudgmentBatch,
    JudgmentPair,
    Login,
    Prepared,
    Rate,
    now,
)
from .schemas import (
    Confirm,
    JudgmentSubmit,
    LivenessComplete,
    LivenessSession,
    Verify,
    Wallet,
)
from .judge_leveling import level_info as judge_level_info
from .judge_leveling import level_for as judge_level_for
from .judge_leveling import rate_multiplier as judge_rate_multiplier
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


def rate(db, key, limit, period=60):
    key = digest(key.encode())
    bucket = now() // period
    insert_once(db, Rate, {"key": key, "bucket": bucket, "count": 0})
    row = lock(db, Rate, key)
    if row.bucket != bucket:
        row.bucket, row.count = bucket, 0
    if row.count >= limit:
        fail(429, "Rate limit reached; retry later")
    row.count += 1


def create_app(settings: Settings, *, chain=None):
    app = FastAPI(title="Capture judgment product", version="0.1.0")
    database = Database(settings.database_url)
    from .db import Base
    from .migrate import apply_migrations

    Base.metadata.create_all(database.engine)
    apply_migrations(database.engine)
    chain = chain or Chain(settings)
    app.state.database, app.state.chain = database, chain

    @app.exception_handler(ChainUnavailable)
    async def unavailable(_, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
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
            from sqlalchemy import inspect

            tables = set(inspect(database.engine).get_table_names())
            required = {
                "seeker_actors",
                "seeker_prepared",
                "judgment_pairs",
                "judgments",
                "calibration_pairs",
            }
            missing = required - tables
            checks["schema"] = "applied" if not missing else "missing " + ",".join(sorted(missing))
            if missing:
                status = 503
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
                "dataset_bias": "majority-preference",
                "skr_mint": settings.skr_mint,
                "program_id": settings.program_id,
                "treasury": (
                    str(chain.treasury.pubkey())
                    if getattr(chain, "treasury", None) is not None
                    else None
                ),
                "slash_authority": (
                    str(chain.verifier.pubkey())
                    if getattr(chain, "verifier", None) is not None
                    else None
                ),
                "skr_payout": (
                    "ready" if getattr(chain, "treasury", None) is not None else "unavailable"
                ),
            },
        )

    @app.post("/auth/challenge")
    def challenge(body: Wallet, request: Request):
        nonce, expires = secrets.token_hex(32), now() + 120
        message = f"{settings.domain} requests Capture judgment test-pilot sign-in.\nWallet: {body.wallet}\nNetwork: {settings.cluster}\nNonce: {nonce}\nExpires: {expires}\nThis does not authorize any token transfer."
        with database.transaction() as db:
            rate(db, "ip:" + (request.client.host if request.client else "unknown"), 30)
            rate(db, "login:" + body.wallet, 10)
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
                valid = Signature.from_string(body.signature).verify(
                    Pubkey.from_string(c.wallet), c.message.encode()
                )
            except ValueError:
                valid = False
            if not valid:
                fail(401, "Signature does not match this wallet and challenge")
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
            "liveness_completed": actor.liveness_completed or not settings.liveness_required,
            "bond_test_skr_units": settings.min_bond,
            "bonded": actor.bonded,
            "skr_funded": actor.usdc_funded,
            "faucet_mints": actor.faucet_count,
            "faucet_max": settings.faucet_max_mints,
            "calibrated": actor.calibrated,
            "staked_skr": actor.staked_usdc,
            "warnings": actor.warnings,
            **level,
        }

    @app.post("/liveness/session")
    def liveness_session(body: LivenessSession, actor: Auth):
        Pubkey.from_string(body.contributor)
        if body.contributor != actor.wallet:
            fail(403, "Liveness contributor must match the authenticated wallet")
        with database.transaction() as db:
            rate(db, "liveness-session:" + actor.wallet, 10, 3600)
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
            digest_ = store.complete(actor.wallet, body.session_id, result_digest)
        except UnknownSession as exc:
            fail(404, str(exc))
        except SessionExpired as exc:
            fail(410, str(exc))
        except SessionAlreadyUsed as exc:
            fail(409, str(exc))
        except LivenessError as exc:
            fail(400, str(exc))
        with database.transaction() as db:
            rate(db, "liveness-complete:" + actor.wallet, 10, 3600)
            worker = lock(db, Actor, actor.wallet)
            worker.liveness_completed = True
        return {
            "liveness_proof_hash": digest_.hex(),
            "task_id": "capture-session",
            "attests": (
                "a live person performed a motion challenge on-device; "
                "not bank-grade anti-spoofing"
            ),
        }

    # ---- Bond (stake) ----

    @app.post("/bond/faucet")
    def bond_faucet(actor: Auth):
        """Devnet test $SKR faucet ("Get test $SKR"). A wallet may tap it up to
        ``faucet_max_mints`` times (each tap mints the minimum bond and drops a
        little SOL). Fails closed (503) so funding is never recorded for a mint
        that did not land."""
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if actor.faucet_count >= settings.faucet_max_mints:
                return {
                    "funded": True,
                    "already": True,
                    "mints": actor.faucet_count,
                    "max": settings.faucet_max_mints,
                }
            try:
                chain.faucet(actor.wallet)
            except ChainUnavailable as exc:
                fail(503, f"Test $SKR faucet unavailable: {exc}")
            actor.usdc_funded = True
            actor.faucet_count += 1
        return {
            "funded": True,
            "already": False,
            "mints": actor.faucet_count,
            "max": settings.faucet_max_mints,
        }

    @app.post("/bond/deposit")
    def bond_deposit(actor: Auth):
        """Build + return the real ``deposit_bond`` prepare transaction. The
        bond is marked ``bonded`` only when the client confirms the exact
        message via /transactions/{id}/confirm."""
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if not actor.usdc_funded:
                fail(409, "Get test $SKR first")
            return prepare(db, "deposit", actor.wallet, actor)

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
            return prepare(db, "withdraw", actor.wallet, actor)

    @app.post("/bond/{action}/prepare")
    def prepare_bond(action: str, actor: Auth):
        if action not in {"deposit", "withdraw"}:
            fail(404, "Unknown action")
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if action == "deposit":
                if not actor.usdc_funded:
                    fail(409, "Get test $SKR first")
            else:
                if not actor.bonded:
                    fail(403, "Bond deposit required")
                if actor.warnings >= 1:
                    fail(403, "Bond has an active warning; withdrawal is frozen")
            return prepare(db, action, actor.wallet, actor)

    # ---- Judgment product (RLHF preference judging) ----

    def rolling_agreement(db, wallet):
        """Rolling agreement rate (fraction) over the last ``judge_window``
        resolved judgments this wallet aligned on. 0.0 when there are none."""
        rows = db.scalars(
            select(Judgment.aligned)
            .where(Judgment.wallet == wallet, Judgment.aligned.is_not(None))
            .order_by(Judgment.submitted_at.desc())
            .limit(settings.judge_window)
        ).all()
        if not rows:
            return 0.0
        return sum(1 for a in rows if a) / len(rows)

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

    def _resolve(pair, js):
        """Pure resolution. Returns ``(winner, aligned_wallets, agreement,
        confidence)`` on a clean resolve, or a ``(reason, None, None, None)``
        sentinel — ``reason`` is ``"neutral"`` when a ``cant_tell``/``tie`` vote
        is present (it must re-route WITHOUT inflating the quorum), or ``"tie"``
        when substantive votes are an even split (needs one tiebreaker judge).

        Neutral votes never count as dissent: they are excluded from the
        winner, the aligned set, the agreement denominator, and the quorum
        (findings #5 and #8)."""
        substantive = [
            j for j in js
            if (pair.task_type == "rating" and j.rating is not None)
            or (pair.task_type != "rating" and j.choice in ("a", "b"))
        ]
        neutral = [
            j for j in js
            if (pair.task_type == "rating" and j.rating is None)
            or (pair.task_type != "rating" and j.choice in ("cant_tell", "tie"))
        ]
        if neutral:
            return "neutral", None, None, None
        if pair.task_type == "rating":
            if not substantive:
                return "tie", None, None, None
            ratings = [j.rating for j in substantive]
            median = judgment_mod.median_rating(ratings)
            aligned = [
                j.wallet
                for j in substantive
                if judgment_mod.is_aligned_rating(j.rating, median)
            ]
            winner = str(median)
        else:
            winner, _votes = judgment_mod.majority_choice(
                [j.choice for j in substantive]
            )
            if winner is None:
                return "tie", None, None, None
            aligned = [j.wallet for j in substantive if j.choice == winner]
        denom = len(substantive)
        agreement = len(aligned) / denom if denom else 0.0
        confidence = agreement * min(1.0, denom / pair.k)
        return winner, aligned, agreement, confidence

    def _flat_payments_recent(db, wallet, limit):
        """Count flat "can't tell" payouts this wallet has already earned, to
        bound abstention farming (finding #12)."""
        if limit <= 0:
            return 0
        return db.scalar(
            select(func.count())
            .select_from(Judgment)
            .where(
                Judgment.wallet == wallet,
                Judgment.choice == "cant_tell",
                Judgment.earned_usdc > 0,
            )
        ) or 0

    def _substantive_judgments(db, pair_id, task_type):
        """Non-consumed substantive judgments for ``pair_id``: a/b votes for a
        preference task, or a 1-5 rating for a rating task. Neutral
        ``cant_tell``/``tie`` votes are excluded — they never advance or inflate
        the quorum (finding #8)."""
        rows = db.scalars(
            select(Judgment).where(
                Judgment.pair_id == pair_id, Judgment.consumed.is_(False)
            )
        ).all()
        if task_type == "rating":
            return [j for j in rows if j.rating is not None]
        return [j for j in rows if j.choice in ("a", "b")]

    def _settle_judgment_obligation(j, pair_id):
        """Mint ``j.earned_usdc`` to ``j.wallet`` and confirm it, then clear the
        settling flag. Idempotent: the memo is the judgment_id, so a retry of
        the same obligation produces a distinct transaction from any other
        obligation (finding #3), and a successful confirm clears settling so the
        next reconciliation does not re-mint (finding #1). Returns True on
        settle, False on a transport failure (obligation stays settling=True)."""
        try:
            sig = chain.settle_skr(j.wallet, j.earned_usdc, memo=j.judgment_id)
        except ChainUnavailable:
            return False
        with database.transaction() as db:
            row = db.get(Judgment, j.judgment_id)
            if row is not None:
                row.payout_signature = sig
                row.settling = False
        return True

    def _maybe_anchor_batch(pair_id):
        """When a batch is fully resolved, compute its Merkle root and commit it
        on-chain via ``post_batch``. The root is always recorded; ``anchored`` is
        set only after a confirmed on-chain write so a failed final anchor is
        durably retried by the next resolution (finding #10)."""
        anchor = None
        with database.transaction() as db:
            pair = db.get(JudgmentPair, pair_id)
            if pair is None or pair.batch_id is None:
                return
            batch = db.get(JudgmentBatch, pair.batch_id)
            if batch is None:
                return
            unresolved = db.scalar(
                select(func.count())
                .select_from(JudgmentPair)
                .where(
                    JudgmentPair.batch_id == pair.batch_id,
                    JudgmentPair.status != "resolved",
                )
            )
            if unresolved:
                return
            resolved = db.scalars(
                select(JudgmentPair).where(
                    JudgmentPair.batch_id == pair.batch_id,
                    JudgmentPair.status == "resolved",
                ).order_by(JudgmentPair.pair_id)
            ).all()
            root = judgment_mod.merkle_root(
                [
                    (p.pair_id, p.preference or "", p.agreement or 0.0, p.confidence or 0.0)
                    for p in resolved
                ]
            )
            if root is None:
                return
            batch.merkle_root = root
            anchor = (batch.batch_id, root)
        try:
            chain.post_batch(anchor[0], anchor[1])
        except ChainUnavailable:
            # Leave anchored=False: the root is recorded and the anchor is
            # retried on the next resolution/reconcile (finding #10).
            return
        with database.transaction() as db:
            b = db.get(JudgmentBatch, anchor[0])
            if b is not None:
                b.anchored = True

    def _enforce_agreement_slash(wallet):
        """Two-strike warn/slash for systematic disagreement (agreement rate
        below the floor). Idempotent via the on-chain bond.warnings counter and
        the Actor.warnings mirror."""
        try:
            warnings = chain.bond_warnings(wallet)
        except ChainUnavailable:
            return
        if warnings == 0:
            try:
                chain.issue("warn", wallet)
            except ChainUnavailable:
                return
            with database.transaction() as db:
                a = lock(db, Actor, wallet)
                a.warnings = 1
        else:
            try:
                if warnings < 2:
                    chain.issue("warn", wallet)
                chain.issue("slash", wallet)
            except ChainUnavailable:
                return
            with database.transaction() as db:
                a = lock(db, Actor, wallet)
                a.warnings = 2
                a.bonded = False
                a.calibrated = False
                a.staked_usdc = 0

    def _maybe_slash_agreement_farmers(judges):
        for wallet, _choice, _rating in judges:
            with database.transaction() as db:
                a = lock(db, Actor, wallet)
                if a is None:
                    continue
                rate_, count = a.agreement_rate, a.judged_count
            if count < settings.judge_slash_min_judged or rate_ >= settings.judge_slash_floor:
                continue
            _enforce_agreement_slash(wallet)

    @app.get("/judge/next")
    def judge_next(actor: Auth):
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            if settings.liveness_required and not actor.liveness_completed:
                return {
                    "kind": "none",
                    "calibrated": True,
                    "reason": "prove you're human before judging",
                }
            if not actor.bonded:
                return {"kind": "none", "calibrated": True, "reason": "deposit the SKR bond to judge"}
            done = select(Judgment.pair_id).where(Judgment.wallet == actor.wallet)
            pair = db.scalars(
                select(JudgmentPair)
                .where(
                    JudgmentPair.status.in_(["open", "collecting", "rerouted"]),
                    JudgmentPair.pair_id.not_in(done),
                )
                .order_by(JudgmentPair.created_at)
                .limit(1)
            ).first()
            if pair is None:
                return {"kind": "none", "calibrated": True}
            view = _pair_view(pair)
            view.update({"kind": "judgment", "calibrated": True, "calibration_remaining": 0})
            return view

    @app.post("/judge/{pair_id}")
    def judge_submit(pair_id: str, body: JudgmentSubmit, actor: Auth):
        with database.transaction() as db:
            is_calibration = db.get(CalibrationPair, pair_id) is not None
        if is_calibration:
            with database.transaction() as db:
                return _submit_calibration(db, pair_id, body, actor.wallet)
        return _submit_judgment(pair_id, body, actor.wallet)

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

    def _settle_and_finalize(pair_id):
        """Settle a resolved pair's pending payout obligations and finalize it.

        Shared by the resolve path and the "resolving" reconciliation path so a
        pair stuck mid-settlement after a partial failure is durably retried
        instead of 409ing forever (finding #3). Reads the resolution from the
        persisted pair columns (set before the first settlement attempt)."""
        with database.transaction() as db:
            pair = lock(db, JudgmentPair, pair_id)
            if pair.status == "resolved":
                return {"status": "resolved", "preference": pair.preference}
            if pair.preference is None:
                fail(409, "Pair is not yet resolved; retry shortly")
            winner = pair.preference
            agreement = pair.agreement
            confidence = pair.confidence
            judges = db.scalars(
                select(Judgment).where(
                    Judgment.pair_id == pair_id, Judgment.consumed.is_(False)
                )
            ).all()
        with database.transaction() as db:
            obligations = db.scalars(
                select(Judgment).where(
                    Judgment.pair_id == pair_id, Judgment.settling.is_(True)
                )
            ).all()
        failed = []
        for j in obligations:
            if not _settle_judgment_obligation(j, pair_id):
                failed.append(j.wallet)
        if failed:
            fail(503, "$SKR payout pending for some judges; retry to reconcile")
        with database.transaction() as db:
            pair = lock(db, JudgmentPair, pair_id)
            if pair.status != "resolved":
                pair.status = "resolved"
                for j in db.scalars(
                    select(Judgment).where(
                        Judgment.pair_id == pair_id, Judgment.consumed.is_(False)
                    )
                ).all():
                    a = lock(db, Actor, j.wallet)
                    if a is not None:
                        a.judged_count += 1
                        a.agreement_rate = rolling_agreement(db, j.wallet)
        _maybe_anchor_batch(pair_id)
        _maybe_slash_agreement_farmers(
            [(j.wallet, j.choice, j.rating) for j in judges]
        )
        return {
            "status": "resolved",
            "preference": winner,
            "agreement": agreement,
            "confidence": confidence,
        }

    def _submit_judgment(pair_id, body, wallet):
        claim = None
        with database.transaction() as db:
            actor = lock(db, Actor, wallet)
            if not actor.bonded:
                fail(403, "Deposit the SKR bond to judge")
            pair = lock(db, JudgmentPair, pair_id)
            if pair.status == "resolved":
                return {"status": "resolved", "preference": pair.preference}
            if pair.status == "resolving":
                # Reconciliation (finding #3): re-run settlement + finalize
                # instead of 409ing forever after a partial payout failure.
                return _settle_and_finalize(pair_id)
            # Finding #4: the local `actor.bonded` is a callback-maintained mirror.
            # A wallet that withdrew its bond on-chain (bypassing the backend
            # confirm endpoint) would otherwise keep judging with an empty,
            # unslashable vault. Re-verify the on-chain bond is live before every
            # judgment: `bond_warnings` raises if the bond account is gone.
            try:
                if chain.bond_warnings(wallet) >= 2:
                    fail(403, "Bond is slashed; deposit a fresh SKR bond to judge")
                if chain.bond_vault_balance(wallet) < settings.min_bond:
                    fail(403, "Bond vault is empty; deposit the SKR bond to judge")
            except ChainUnavailable:
                fail(403, "Bond is not active on-chain; deposit the SKR bond to judge")
            choice, rating = _validate_against_task(pair.task_type, body)
            existing = db.scalar(
                select(Judgment).where(
                    Judgment.pair_id == pair_id, Judgment.wallet == wallet
                )
            )
            if existing:
                fail(409, "You already judged this pair")
            db.add(Judgment(pair_id=pair_id, wallet=wallet, choice=choice, rating=rating))
            db.flush()
            substantive = _substantive_judgments(db, pair_id, pair.task_type)
            if len(substantive) < pair.k:
                if pair.status == "open":
                    pair.status = "collecting"
                if choice in ("cant_tell", "tie") or (pair.task_type == "rating" and rating is None):
                    jj = db.scalar(
                        select(Judgment).where(
                            Judgment.pair_id == pair_id, Judgment.wallet == wallet
                        )
                    )
                    jj.consumed = True
                    claim = {"type": "neutral", "flat": [wallet] if choice == "cant_tell" else []}
                else:
                    return {"status": "submitted"}
            else:
                js = db.scalars(
                    select(Judgment).where(
                        Judgment.pair_id == pair_id, Judgment.consumed.is_(False)
                    )
                ).all()
                resolution = _resolve(pair, js)
                reason = resolution[0] if isinstance(resolution[0], str) else None
                if reason == "neutral":
                    for j in js:
                        if j.choice in ("cant_tell", "tie") or (pair.task_type == "rating" and j.rating is None):
                            j.consumed = True
                    pair.status = "collecting"
                    claim = {"type": "neutral", "flat": [j.wallet for j in js if j.choice == "cant_tell"]}
                elif reason == "tie":
                    pair.status = "rerouted"
                    pair.k += 1
                    claim = {"type": "tie"}
                else:
                    winner, aligned, agreement, confidence = resolution
                    payouts = []
                    for w in aligned:
                        lvl = judge_level_for(rolling_agreement(db, w))
                        payouts.append((w, int(settings.judge_base_skr * judge_rate_multiplier(lvl))))
                    payout_map = dict(payouts)
                    for j in db.scalars(
                        select(Judgment).where(
                            Judgment.pair_id == pair_id, Judgment.consumed.is_(False)
                        )
                    ).all():
                        j.aligned = j.wallet in aligned
                        if j.aligned:
                            j.earned_usdc = payout_map[j.wallet]
                            j.settling = True
                    pair.status = "resolving"
                    pair.preference = winner
                    pair.agreement = agreement
                    pair.confidence = confidence
                    claim = {
                        "type": "resolve",
                        "winner": winner,
                        "aligned": aligned,
                        "agreement": agreement,
                        "confidence": confidence,
                        "payouts": payouts,
                        "judges": [(j.wallet, j.choice, j.rating) for j in js],
                    }
        if claim["type"] == "neutral":
            for w in claim["flat"]:
                j = None
                with database.transaction() as db:
                    j = db.scalar(
                        select(Judgment).where(Judgment.pair_id == pair_id, Judgment.wallet == w)
                    )
                    if j is not None and not j.settling:
                        earned = _flat_payments_recent(db, w, settings.judge_flat_cap)
                        if settings.judge_flat_cap > 0 and earned >= settings.judge_flat_cap:
                            j.consumed = True
                            continue
                        j.earned_usdc = settings.judge_flat_skr
                        j.settling = True
                if j is not None and j.settling:
                    _settle_judgment_obligation(j, pair_id)
            return {"status": "rerouted"}
        if claim["type"] == "tie":
            return {"status": "rerouted"}
        return _settle_and_finalize(pair_id)

    @app.get("/judge/stats")
    def judge_stats(actor: Auth):
        with database.transaction() as db:
            actor = lock(db, Actor, actor.wallet)
            answered, correct = calibration_progress(db, actor.wallet)
            level = judge_level_info(actor.agreement_rate)
            in_progress = (not actor.calibrated) and 0 < answered < settings.calibration_n
            earned = db.scalar(
                select(func.coalesce(func.sum(Judgment.earned_usdc), 0)).where(
                    Judgment.wallet == actor.wallet
                )
            )
            settled = db.scalar(
                select(func.coalesce(func.sum(Judgment.earned_usdc), 0)).where(
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
                "staked_skr": actor.staked_usdc,
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

    def prepare(db, action, target, actor):
        old = db.scalars(
            select(Prepared)
            .where(
                Prepared.wallet == actor.wallet,
                Prepared.action == action,
                Prepared.target == target,
                Prepared.confirmed.is_(False),
            )
            .order_by(Prepared.last_valid_height.desc())
        ).first()
        if old and old.last_valid_height >= chain.height():
            return tx_view(old)
        result = chain.prepare(action, actor.wallet)
        p = Prepared(wallet=actor.wallet, action=action, target=target, **result)
        db.add(p)
        db.flush()
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

        # Phase 3: re-acquire the lock and write atomically, tolerating a
        # concurrent confirmation that landed between phases.
        with database.transaction() as db:
            p = lock(db, Prepared, pid)
            if p.confirmed:
                if signature and signature != p.signature:
                    fail(409, "Already confirmed with another signature")
                return tx_view(p)
            p.signature, p.confirmed = resolved, True
            if p.action == "deposit":
                worker = lock(db, Actor, p.wallet)
                worker.bonded = True
                worker.staked_usdc = settings.min_bond
            elif p.action == "withdraw":
                worker = lock(db, Actor, p.wallet)
                worker.bonded = False
                worker.staked_usdc = 0
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
