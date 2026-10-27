"""Durable fixed-window request admission, independent of route transactions.

All workers use atomic database upserts. Counters are committed even when auth,
validation, chain operations or the handler subsequently fail. Only the ASGI
peer address is trusted; configure the server's proxy trust explicitly.
"""

import asyncio
import hashlib
import json

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from solders.pubkey import Pubkey

from .db import Challenge, Login, Rate, now


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


class RateLimiter:
    def __init__(self, database):
        self.database = database

    def consume(self, budgets):
        """Return Retry-After seconds, or zero. Never raise inside the transaction
        for a denied request: that would roll back the budget it consumed.
        Budgets are (stable key, limit, period seconds), ordered global first.
        """
        retry = 0
        with self.database.transaction() as db:
            postgres = db.bind.dialect.name == "postgresql"
            timestamp = int(db.scalar(select(func.extract("epoch", func.clock_timestamp())))) if postgres else now()
            insert = pg_insert if postgres else sqlite_insert
            for key, limit, period in budgets:
                bucket = timestamp // period
                count, stored_bucket = db.execute(
                    insert(Rate).values(key=_key(key), bucket=bucket, count=1)
                    .on_conflict_do_update(
                        index_elements=[Rate.key],
                        set_={
                            # A delayed transaction or clock correction must
                            # never move a counter back into an earlier window.
                            "bucket": case((Rate.bucket < bucket, bucket), else_=Rate.bucket),
                            "count": case(
                                (Rate.bucket < bucket, 1),
                                (Rate.count <= limit, Rate.count + 1),
                                else_=Rate.count,
                            ),
                        },
                    ).returning(Rate.count, Rate.bucket)
                ).one()
                if count > limit:
                    retry = (stored_bucket + 1) * period - timestamp
                    # Do not allocate attacker-controlled IP/wallet keys once
                    # an enclosing budget is exhausted.
                    break
        return retry

    def wallet(self, authorization, path, body):
        if not authorization.startswith("Bearer ") and path not in {"/auth/challenge", "/auth/verify"}:
            return None
        with self.database.transaction() as db:
            if authorization.startswith("Bearer "):
                login = db.get(Login, _key(authorization[7:]))
                # Expired sessions still consume their wallet's budget. This
                # lookup grants no authentication; the route verifies expiry.
                if login:
                    return login.wallet
            if path not in {"/auth/challenge", "/auth/verify"}:
                return None
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    return None
                if path == "/auth/challenge":
                    wallet = data.get("wallet")
                    if isinstance(wallet, str) and len(wallet) <= 44:
                        Pubkey.from_string(wallet)
                        return wallet
                challenge_id = data.get("challenge_id")
                if path == "/auth/verify" and isinstance(challenge_id, str) and len(challenge_id) <= 64:
                    challenge = db.get(Challenge, challenge_id)
                    return challenge.wallet if challenge else None
            except (ValueError, UnicodeError, RecursionError):
                pass
        return None


def _category(path):
    # Share route budgets across IDs and aliases; path churn cannot reset them.
    if path.startswith("/auth/"):
        return "auth"
    if path.startswith("/liveness/"):
        return "liveness-complete" if path == "/liveness/complete" else "liveness-session"
    if path.startswith(("/bond/", "/transactions")):
        return "bond"
    if path.startswith("/judge/"):
        return "judge"
    return "general"


class RequestLimitsMiddleware:
    def __init__(self, app, database, settings):
        self.app, self.settings = app, settings
        self.limiter = RateLimiter(database)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = self.settings
        path = scope["path"].rstrip("/")
        category = _category(path)
        peer = scope.get("client")
        ip = peer[0] if peer else "unknown"

        async def reject(status, detail, retry=0):
            response = JSONResponse(
                {"detail": detail}, status_code=status,
                headers={"Retry-After": str(retry)} if retry else None,
            )
            await response(scope, receive, send)

        async def consume(budgets):
            retry = await run_in_threadpool(self.limiter.consume, budgets)
            if retry:
                await reject(429, "Rate limit reached; retry later", retry)
            return bool(retry)

        try:
            budgets = [
                ("global", settings.rate_global_per_minute, 60),
                ("ip:" + ip, settings.rate_ip_per_minute, 60),
            ]
            if category != "general":
                limit = settings.rate_auth_ip_per_minute if category == "auth" else settings.rate_expensive_ip_per_minute
                budgets.append((f"ip:{category}:{ip}", limit, 60))
            if await consume(budgets):
                return

            headers = scope.get("headers", [])
            head_size = len(scope.get("raw_path", path.encode())) + len(scope.get("query_string", b""))
            head_size += sum(len(k) + len(v) + 4 for k, v in headers)
            if head_size > settings.max_request_head_bytes:
                return await reject(431, "Request headers or target too large")
            for name, value in headers:
                if name.lower() == b"content-length":
                    try:
                        size = int(value)
                    except ValueError:
                        return await reject(400, "Invalid Content-Length")
                    if size < 0:
                        return await reject(400, "Invalid Content-Length")
                    if size > settings.max_request_body_bytes:
                        return await reject(413, "Request body too large")

            async def read_body():
                data = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return None
                    chunk = message.get("body", b"")
                    if len(data) + len(chunk) > settings.max_request_body_bytes:
                        raise OverflowError
                    data.extend(chunk)
                    if not message.get("more_body", False):
                        return bytes(data)

            try:
                body = await asyncio.wait_for(read_body(), settings.request_body_timeout_seconds)
            except OverflowError:
                return await reject(413, "Request body too large")
            except asyncio.TimeoutError:
                return await reject(408, "Request body timed out")
            if body is None:
                return
            authorization = next((v.decode("latin-1") for k, v in headers if k.lower() == b"authorization"), "")
            wallet = await run_in_threadpool(self.limiter.wallet, authorization, path, body)
            if wallet:
                budgets = [("wallet:" + wallet, settings.rate_wallet_per_minute, 60)]
                policy = {
                    "auth": (20, 60), "bond": (30, 60), "judge": (60, 60),
                    "liveness-session": (10, 3600), "liveness-complete": (10, 3600),
                }
                if category in policy:
                    limit, period = policy[category]
                    budgets.append((f"wallet:{category}:{wallet}", limit, period))
                if await consume(budgets):
                    return
        except SQLAlchemyError:
            return await reject(503, "Request budget store unavailable")

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
