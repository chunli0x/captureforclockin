"""Worker-only settlement. Every chain call is between short fenced transactions."""
from sqlalchemy import select, func

from .db import Job, Actor, Judgment, JudgmentPair, JudgmentBatch, Prepared
from .outbox import enqueue, Waiting
from . import judgment as judgment_mod
from .judge_leveling import level_for, rate_multiplier


def lock(db, cls, key):
    pk = next(iter(cls.__table__.primary_key.columns))
    return db.scalar(select(cls).where(pk == key).with_for_update().execution_options(populate_existing=True))


def rolling_agreement(db, wallet, settings):
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
            Judgment.earned_skr > 0,
        )
    ) or 0


def _substantive_judgments(db, pair_id, task_type):
    """Non-consumed substantive judgments for ``pair_id``: a/b votes for a
    preference task, or a 1-5 rating for a rating task. Neutral
    ``cant_tell``/``tie`` votes are excluded — they never advance or inflate
    the quorum (finding #8)."""
    rows = db.scalars(
        select(Judgment).where(
            Judgment.pair_id == pair_id, Judgment.consumed.is_(False),
            Judgment.liability_reserved.is_(True),
        )
    ).all()
    if task_type == "rating":
        return [j for j in rows if j.rating is not None]
    return [j for j in rows if j.choice in ("a", "b")]


class Settlement:
    def __init__(self, worker, lease):
        self.worker, self.lease = worker, lease
        self.chain, self.settings = worker.chain, worker.settings

    def transaction(self):
        return self.worker.transaction(self.lease)

    def faucet(self, wallet):
        with self.transaction() as db:
            job = db.get(Job, self.lease.id)
            if job.payload.get("signature"):
                return
            prepared = job.payload.get("prepared")

        def persist(prepared):
            with self.transaction() as db:
                job = db.get(Job, self.lease.id)
                job.payload = {**job.payload, "prepared": prepared}

        signature = self.chain.faucet(wallet, persist=persist, prepared=prepared)
        with self.transaction() as db:
            job = db.get(Job, self.lease.id)
            job.payload = {**job.payload, "signature": signature}
            lock(db, Actor, wallet).skr_funded = True

    def reservation(self, jid):
        with self.transaction() as db:
            j = db.get(Judgment, jid)
            pair = db.get(JudgmentPair, j.pair_id)
            if j.enforcement_requested or j.enforcement_complete or pair.status in ("resolving", "enforcing", "resolved"):
                enqueue(db, "settlement", j.pair_id)
                return
            if j.liability_legacy and j.consumed:
                j.liability_legacy = False
                j.enforcement_requested = True
                enqueue(db, "settlement", j.pair_id)
                return
            reserved = j.liability_reserved
        if not reserved:
            # The program atomically checks the collateral and reserves it;
            # checking a balance first would introduce a withdrawal race.
            self.chain.reserve_judgment(j.wallet, jid)
        with self.transaction() as db:
            pair = lock(db, JudgmentPair, j.pair_id)
            row = lock(db, Judgment, jid)
            if pair.status not in ("resolving", "enforcing", "resolved"):
                row.liability_reserved = True
                row.liability_legacy = False
            enqueue(db, "settlement", row.pair_id)

    def _decide(self, pair_id):
        settings = self.settings
        with self.transaction() as db:
            pair = lock(db, JudgmentPair, pair_id)
            if pair.status in ("resolving", "enforcing", "resolved"):
                return
            votes = list(db.scalars(select(Judgment).where(Judgment.pair_id == pair_id).order_by(Judgment.wallet)))
            # Pre-upgrade accepted votes must all be reserved before new votes
            # can resolve their pair, as in the former legacy reservation gate.
            if any(j.liability_legacy and not j.consumed for j in votes):
                raise Waiting()
            # Lock wallets in deterministic order: neutral caps and histories
            # are shared across pairs, including pairs on different workers.
            for wallet in sorted({j.wallet for j in votes}):
                lock(db, Actor, wallet)
            for j in votes:
                if not j.liability_reserved or j.consumed:
                    continue
                neutral = (j.rating is None if pair.task_type == "rating" else j.choice in ("cant_tell", "tie"))
                if neutral:
                    j.consumed = True
                    j.enforcement_requested = True
                    if j.choice == "cant_tell":
                        earned = _flat_payments_recent(db, j.wallet, settings.judge_flat_cap)
                        if settings.judge_flat_cap == 0 or earned < settings.judge_flat_cap:
                            j.earned_skr = settings.judge_flat_skr
                            j.settling = j.earned_skr > 0
            db.flush()
            substantive = _substantive_judgments(db, pair_id, pair.task_type)
            if len(substantive) >= pair.k:
                winner, aligned, agreement, confidence = _resolve(pair, substantive)
                if winner == "tie":
                    pair.status = "rerouted"
                    pair.k += 1
                else:
                    payouts = {wallet: int(settings.judge_base_skr * rate_multiplier(
                        level_for(rolling_agreement(db, wallet, settings)))) for wallet in aligned}
                    for vote in substantive:
                        vote.aligned = vote.wallet in aligned
                        if vote.aligned:
                            vote.earned_skr = payouts[vote.wallet]
                            vote.settling = vote.earned_skr > 0
                    pair.status = "resolving"
                    pair.preference, pair.agreement, pair.confidence = winner, agreement, confidence
            elif pair.status == "open":
                pair.status = "collecting"

    def _payout(self, j):
        def persist(prepared):
            with self.transaction() as db:
                row = lock(db, Judgment, j.judgment_id)
                row.payout_transaction = prepared["transaction"]
                row.payout_pending_signature = prepared["signature"]
                row.payout_last_valid_height = prepared["last_valid_height"]
        prepared = None
        if j.payout_transaction:
            prepared = {"transaction": j.payout_transaction, "signature": j.payout_pending_signature,
                        "last_valid_height": j.payout_last_valid_height}
        # Obligations currently have one immutable version. The receipt binds it;
        # a future version cannot create a second receipt for the same judgment ID.
        settle = (self.chain.settle_transfer if self.settings.payout_mode == "transfer"
                  else self.chain.settle_skr)
        sig = settle(j.wallet, j.earned_skr, memo=j.judgment_id,
                                    persist=persist, prepared=prepared)
        with self.transaction() as db:
            row = lock(db, Judgment, j.judgment_id)
            row.payout_signature, row.settling = sig, False

    def settlement(self, pair_id):
        self._decide(pair_id)
        with self.transaction() as db:
            obligations = list(db.scalars(select(Judgment).where(
                Judgment.pair_id == pair_id, Judgment.settling.is_(True)).order_by(Judgment.wallet)))
        for j in obligations:
            self._payout(j)
        with self.transaction() as db:
            pair = lock(db, JudgmentPair, pair_id)
            if pair.status == "resolving":
                # Transition and decisions commit once, AFTER every payout.
                pair.status = "enforcing"
                judges = list(db.scalars(select(Judgment).where(
                    Judgment.pair_id == pair_id).order_by(Judgment.wallet)))
                for j in judges:
                    if not j.liability_reserved:
                        j.consumed = True
                    if not j.consumed:
                        actor = lock(db, Actor, j.wallet)
                        actor.judged_count += 1
                        actor.agreement_rate = rolling_agreement(db, j.wallet, self.settings)
                        j.enforcement_penalty = (
                            actor.judged_count >= self.settings.judge_slash_min_judged
                            and actor.agreement_rate < self.settings.judge_slash_floor)
                    j.enforcement_requested = True
            if pair.status == "resolved":
                # Historical finalized judgments may predate liability journals.
                # Release/tombstone them without re-scoring or new penalties.
                for j in db.scalars(select(Judgment).where(
                    Judgment.pair_id == pair_id, Judgment.enforcement_requested.is_(False),
                    Judgment.enforcement_complete.is_(False))):
                    j.enforcement_requested = True
            pending = list(db.scalars(select(Judgment).where(
                Judgment.pair_id == pair_id, Judgment.enforcement_requested.is_(True),
                Judgment.enforcement_complete.is_(False)).order_by(Judgment.wallet)))
        for j in pending:
            warnings = self.chain.resolve_judgment(j.wallet, j.judgment_id, j.enforcement_penalty)
            with self.transaction() as db:
                actor = lock(db, Actor, j.wallet)
                row = lock(db, Judgment, j.judgment_id)
                row.enforcement_complete = True
                # Another pair may have advanced this wallet in the meantime.
                actor.warnings = max(actor.warnings, min(warnings, 2))
                if warnings >= 2:
                    actor.bonded, actor.calibrated, actor.staked_skr = False, False, 0
        with self.transaction() as db:
            pair = lock(db, JudgmentPair, pair_id)
            if pair.status in ("enforcing", "resolved"):
                pair.status = "resolved"
                enqueue(db, "anchor", pair.batch_id)
                return
        raise Waiting()

    def anchor(self, batch_id):
        with self.transaction() as db:
            batch = lock(db, JudgmentBatch, batch_id)
            if batch.anchored:
                return
            pairs = list(db.scalars(select(JudgmentPair).where(
                JudgmentPair.batch_id == batch_id).order_by(JudgmentPair.pair_id)))
            if not pairs or any(p.status != "resolved" for p in pairs):
                raise Waiting()
            root = judgment_mod.merkle_root([(p.pair_id, p.preference or "", p.agreement or 0.0,
                                             p.confidence or 0.0) for p in pairs])
            if batch.merkle_root and batch.merkle_root != root:
                raise ValueError("Immutable batch root mismatch")
            batch.merkle_root = root
        self.chain.post_batch(batch_id, root)
        with self.transaction() as db:
            lock(db, JudgmentBatch, batch_id).anchored = True

    def prepared(self, pid):
        with self.transaction() as db:
            p = db.get(Prepared, pid)
            if p.confirmed:
                return
        signature = p.signature or self.chain.find_signature(p)
        self.chain.confirm(p, signature)
        warnings = self.chain.bond_warnings(p.wallet) if p.action == "deposit" else None
        with self.transaction() as db:
            actor = lock(db, Actor, p.wallet)
            row = lock(db, Prepared, pid)
            if row.confirmed:
                return
            row.signature, row.confirmed = signature, True
            actor.bonded = row.action == "deposit"
            if actor.bonded:
                actor.warnings = min(warnings, 2)
            actor.staked_skr = self.settings.min_bond if actor.bonded else 0
