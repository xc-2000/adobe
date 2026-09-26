"""Background generation worker. Redis list is the queue; FastAPI lifespan runs the loop.

Jobs are billed at submit time. This worker only settles or refunds through lifecycle helpers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import func, select

from app.adobe.client import (
    AdobeGenerateError,
    adobe_result_urls,
    generate_image,
    generate_video,
    upload_reference_image,
)
from app.adobe.ff_ios import FF_IOS_UA
from app.adobe.http import is_transient_connect, optional_proxy_url
from app.adobe.payloads import build_image_payload, build_video_payload
from app.adobe.refs import load_reference_image
from app.adobe.trace import UpstreamTrace
from app.arp.store import ArpStore
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import redact
from app.core.resources import Resources
from app.models import GenerationJob
from app.models.account import RequestLog, UpstreamAccount
from app.services import accounts as account_pool
from app.services.config.proxy import build_session
from app.services.config.store import ConfigStore
from app.services.generation import lifecycle
from app.services.generation.errors import public_fail_message
from app.services.storage import store_image

logger = logging.getLogger(__name__)
QUEUE_KEY = "firefly:jobs:queued"
DELAYED_KEY = "firefly:jobs:delayed"
CAPACITY_WAIT_SECONDS = 5.0
ALPHA_MODELS = frozenset({"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"})
_CAPACITY_CODES = frozenset({"no_account", "arp_empty"})
_TIMEOUT_ERROR = AdobeGenerateError("poll_timeout", 504, "generation timeout")
_PROMOTE_DELAYED = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for i = 1, #due do
  redis.call('ZREM', KEYS[1], due[i])
  redis.call('LPUSH', KEYS[2], due[i])
end
return #due
"""


def identity_lane(model: str, *, dual_identity: bool) -> str:
    """web = clio + ARP; mobile = FF-iOS. Account group is not used."""
    if dual_identity and model.strip().lower() not in ALPHA_MODELS:
        return "mobile"
    return "web"


async def _checkout_for_job(
    session: Any,
    redis: Any,
    cfg: Any,
    *,
    model: str,
    resolution: str,
    dual_identity: bool,
    sticky_account_id: int | None,
    in_window: bool,
) -> Any:
    kwargs: dict[str, Any] = {
        "daily_limit": cfg.account_daily_image_limit,
        "max_leases": (
            cfg.account_concurrency_advanced
            if str(resolution or "") in {"4K", "4k"}
            else cfg.account_concurrency_normal
        ),
        "advanced": str(resolution or "") in {"4K", "4k"},
        "model": model,
        "block_models": cfg.blocked_408_models(),
        "block_after": cfg.account_408_block_after,
        "force_single": in_window,
    }
    if sticky_account_id is not None:
        return await account_pool.checkout(
            session, redis, account_id=sticky_account_id, group=None, **kwargs
        )
    canary = 0
    if dual_identity:
        lane = identity_lane(model, dual_identity=True)
        canary = (
            cfg.alpha_canary_account_id if lane == "web" else cfg.normal_canary_account_id
        )
    return await account_pool.checkout(
        session, redis, group=None, account_id=(canary or None), **kwargs
    )


_RETRYABLE = frozenset(
    {
        "arp_rejected",
        "rate_limited",
        "content_rejected",
        "upstream_error",
        "poll_timeout",
        "download_failed",
        "proxy_tls",
    }
)


def _upload_reference(
    *,
    token: str,
    media_dir: Path,
    credentials: Any,
    user_agent: str,
    impersonate: str,
    proxy_url: str | None,
    trace: UpstreamTrace | None = None,
) -> str:
    content, content_type = load_reference_image(token, media_dir=media_dir)
    return upload_reference_image(
        content=content,
        content_type=content_type,
        credentials=credentials,
        user_agent=user_agent,
        impersonate=impersonate,
        proxy_url=proxy_url,
        trace=trace,
    )


async def enqueue(redis: Any, job_id: str) -> None:
    await redis.lpush(QUEUE_KEY, job_id)


async def enqueue_later(redis: Any, job_id: str, delay_seconds: float) -> None:
    when = time.time() + max(0.1, float(delay_seconds))
    await redis.zadd(DELAYED_KEY, {job_id: when})


async def promote_delayed(redis: Any) -> int:
    raw = await redis.eval(_PROMOTE_DELAYED, 2, DELAYED_KEY, QUEUE_KEY, str(time.time()))
    return int(raw or 0)


async def _logged_submit_attempts(session: Any, job_id: str) -> int:
    n = await session.scalar(
        select(func.count())
        .select_from(RequestLog)
        .where(
            RequestLog.job_id == job_id,
            RequestLog.outcome.notin_(tuple(_CAPACITY_CODES)),
        )
    )
    return int(n or 0)


def _attempts_used(job: GenerationJob, logged: int) -> int:
    return max(int(job.retry_count or 0), logged)


def _generation_age_seconds(job: GenerationJob) -> float:
    """Wall clock from job create (queue wait counts toward generation_timeout)."""
    started = job.created_at
    if started is None:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (datetime.now(UTC) - started).total_seconds()


def _generation_timed_out(job: GenerationJob, timeout_seconds: int) -> bool:
    return _generation_age_seconds(job) >= timeout_seconds


async def _fail_generation_timeout(session: Any, job: GenerationJob) -> bool:
    if job.status not in {"queued", "running"}:
        return False
    await lifecycle.mark_failed(
        session,
        job,
        code="poll_timeout",
        message=public_fail_message("poll_timeout"),
    )
    return True


async def sweep_timed_out_jobs(app: FastAPI) -> int:
    """Fail open jobs past generation_timeout even if they vanished from Redis."""
    resources: Resources = app.state.resources
    cfg = await _config_store(app).load()
    failed = 0
    async with resources.request_sessions() as session:
        jobs = (
            await session.execute(
                select(GenerationJob).where(GenerationJob.status.in_(("queued", "running")))
            )
        ).scalars().all()
        for job in jobs:
            if not _generation_timed_out(job, cfg.generation_timeout_seconds):
                continue
            try:
                if await _fail_generation_timeout(session, job):
                    failed += 1
            except AppError:
                logger.warning("timeout sweep skipped job %s status=%s", job.id, job.status)
        await session.commit()
    if failed:
        logger.info("generation timeout sweep failed %s jobs", failed)
    return failed


async def reclaim_open_jobs(app: FastAPI) -> int:
    """Re-queue queued/running jobs missing from Redis after a worker restart."""
    resources: Resources = app.state.resources
    redis = resources.redis
    queued = await redis.lrange(QUEUE_KEY, 0, -1)
    delayed = await redis.zrange(DELAYED_KEY, 0, -1)
    known = {
        item.decode() if isinstance(item, (bytes, bytearray)) else str(item)
        for item in list(queued or []) + list(delayed or [])
    }
    async with resources.request_sessions() as session:
        ids = (
            await session.execute(
                select(GenerationJob.id).where(GenerationJob.status.in_(("queued", "running")))
            )
        ).scalars().all()
        await session.commit()
    n = 0
    for job_id in ids:
        if str(job_id) in known:
            continue
        await enqueue(redis, str(job_id))
        n += 1
    if n:
        logger.info("reclaimed %s open generation jobs onto queue", n)
    return n


async def timeout_watchdog_loop(app: FastAPI, *, interval_seconds: float = 5.0) -> None:
    settings: Settings = app.state.settings
    if not settings.gen_worker_enabled:
        return
    while True:
        try:
            await sweep_timed_out_jobs(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("generation timeout sweep skipped", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def _park_for_capacity(
    app: FastAPI,
    *,
    job_id: str,
    code: str,
    timeout_seconds: int,
) -> bool:
    """Put a running job back on the delayed queue. False if the wait budget is spent."""
    resources: Resources = app.state.resources
    async with resources.request_sessions() as session:
        job = await session.get(GenerationJob, job_id)
        if job is None or job.status not in {"queued", "running"}:
            await session.commit()
            return True
        if _generation_timed_out(job, timeout_seconds):
            await session.commit()
            return False
        if job.status == "running":
            await lifecycle.mark_queued(session, job)
        await session.commit()
    await enqueue_later(resources.redis, job_id, CAPACITY_WAIT_SECONDS)
    logger.info("job %s waiting for capacity code=%s", job_id, code)
    return True


def _arp_store(app: FastAPI, ttl: int, max_uses: int) -> ArpStore:
    settings: Settings = app.state.settings
    resources: Resources = app.state.resources
    return ArpStore(
        resources.redis,
        key_prefix=settings.arp_key_prefix,
        ttl_seconds=ttl,
        max_uses=max_uses,
    )


def _config_store(app: FastAPI) -> ConfigStore:
    existing = getattr(app.state, "config_store", None)
    if isinstance(existing, ConfigStore):
        return existing
    settings: Settings = app.state.settings
    resources: Resources = app.state.resources
    store = ConfigStore(
        resources.sessions,
        resources.redis,
        resources.cipher,
        cache_seconds=settings.config_cache_seconds,
        channel=settings.config_channel,
    )
    app.state.config_store = store
    return store


async def _log(
    session: Any,
    *,
    job_id: str,
    account_hash: str,
    egress_id: str,
    arp_id: str | None,
    status_code: int,
    latency_ms: int,
    retry_count: int,
    outcome: str,
    trace: list[dict[str, Any]] | None = None,
) -> None:
    session.add(
        RequestLog(
            job_id=job_id,
            account_hash=account_hash,
            egress_id=egress_id,
            endpoint="generate",
            status_code=status_code,
            latency_ms=latency_ms,
            retry_count=retry_count,
            arp_id=arp_id,
            outcome=outcome,
            trace=list(trace or []),
        )
    )


async def process_job(app: FastAPI, job_id: str) -> None:
    resources: Resources = app.state.resources
    settings: Settings = app.state.settings
    store = _config_store(app)
    cfg = await store.load()
    media_dir = Path(settings.media_dir)

    async with resources.request_sessions() as session:
        job = await session.get(GenerationJob, job_id)
        if job is None:
            return
        if job.status not in {"queued", "running"}:
            await session.commit()
            return
        if _generation_timed_out(job, cfg.generation_timeout_seconds):
            await _fail_generation_timeout(session, job)
            await session.commit()
            return
        used = _attempts_used(job, await _logged_submit_attempts(session, job_id))
        max_attempts = cfg.retry_max_attempts
        if used >= max_attempts:
            if job.status == "queued":
                await lifecycle.mark_running(session, job)
            await lifecycle.mark_failed(
                session,
                job,
                code="poll_timeout",
                message=public_fail_message("poll_timeout"),
            )
            await session.commit()
            return
        if job.status == "queued":
            await lifecycle.mark_running(session, job)
            await session.commit()

    dual_identity = cfg.adobe_dual_identity_enabled
    lane = identity_lane(job.model, dual_identity=dual_identity)
    web_lane = lane == "web"
    sticky_account_id: int | None = None
    sticky_credentials: Any | None = None
    last_error = AdobeGenerateError("upstream_error", 500, "generation failed")
    for attempt in range(used, max_attempts):
        bundle = None
        account = None
        proxy_url = ""
        egress_id = ""
        started = time.perf_counter()
        trace = UpstreamTrace()
        try:
            async with resources.request_sessions() as session:
                job = await session.get(GenerationJob, job_id)
                if job is None or job.status != "running":
                    await session.commit()
                    return
                if _generation_timed_out(job, cfg.generation_timeout_seconds):
                    last_error = _TIMEOUT_ERROR
                    await session.commit()
                    break
                in_window = await account_pool.bad_window_active(resources.redis)
                account = await _checkout_for_job(
                    session,
                    resources.redis,
                    cfg,
                    model=job.model,
                    resolution=str(job.params.get("resolution") or ""),
                    dual_identity=dual_identity,
                    sticky_account_id=sticky_account_id,
                    in_window=in_window,
                )
                if account is None:
                    raise AdobeGenerateError("no_account", 503, "no upstream account available")
                if web_lane:
                    arp = _arp_store(app, cfg.arp_ttl_seconds, cfg.arp_max_uses)
                    health = await arp.health()
                    if int(health.get("ready") or 0) < cfg.arp_min_ready and attempt == 0:
                        logger.info("arp pool below waterline ready=%s", health.get("ready"))
                    bundle = await arp.checkout()
                    if bundle is None:
                        raise AdobeGenerateError("arp_empty", 503, "ARP pool is empty")
                    proxy_url = bundle.proxy_url.get_secret_value()
                    if not proxy_url.strip():
                        await arp.invalidate(bundle.id)
                        raise AdobeGenerateError(
                            "proxy_required", 422, "ARP bundle missing proxy"
                        )
                    egress_id = bundle.egress_id
                else:
                    password = store.proxy_password(cfg)
                    if not cfg.proxy.host or not cfg.proxy.username or not password:
                        raise AdobeGenerateError(
                            "proxy_required", 422, "mobile identity requires configured proxy"
                        )
                    proxy = build_session(
                        cfg.proxy, password, arp_ttl_seconds=cfg.arp_ttl_seconds
                    )
                    proxy_url = proxy.url
                    egress_id = proxy.egress_id
                if sticky_credentials is not None and web_lane:
                    creds = sticky_credentials
                else:
                    creds = await account_pool.credentials_for_account(
                        session,
                        account,
                        resources.cipher,
                        proxy_url=proxy_url,
                        user_agent=bundle.ua if bundle is not None else FF_IOS_UA,
                        impersonate=settings.submit_impersonate,
                        identity=lane,
                        trace=trace,
                    )
                    if web_lane:
                        sticky_account_id = account.id
                        sticky_credentials = creds
                trace.record(
                    "identity",
                    body={"lane": lane, "model": job.model},
                )
                blob_ids: list[str] = []
                upload_proxy = optional_proxy_url(
                    cfg.proxy.upload_via_proxy,
                    proxy_url,
                )
                for item in list(job.params.get("reference_blob_ids") or []):
                    token = str(item).strip()
                    if not token:
                        continue
                    blob_ids.append(
                        await asyncio.to_thread(
                            _upload_reference,
                            token=token,
                            media_dir=media_dir,
                            credentials=creds,
                            user_agent=bundle.ua if bundle is not None else FF_IOS_UA,
                            impersonate=settings.submit_impersonate,
                            proxy_url=upload_proxy,
                            trace=trace,
                        )
                    )
                if job.kind == "video":
                    payload = build_video_payload(
                        model=job.model,
                        prompt=job.prompt,
                        resolution=str(job.params.get("resolution") or "720p"),
                        aspect_ratio=str(job.params.get("aspect_ratio") or "16:9"),
                        duration=int(job.params.get("duration") or 8),
                        reference_blob_ids=blob_ids,
                    )
                else:
                    payload = build_image_payload(
                        model=job.model,
                        prompt=job.prompt,
                        resolution=str(job.params.get("resolution") or "1K"),
                        aspect_ratio=str(job.params.get("aspect_ratio") or "1:1"),
                        reference_blob_ids=blob_ids,
                    )
                kind = job.kind
                job.retry_count = attempt + 1
                await session.commit()

            remaining = int(cfg.generation_timeout_seconds - _generation_age_seconds(job))
            if remaining <= 0:
                last_error = _TIMEOUT_ERROR
                break
            generate = generate_video if kind == "video" else generate_image
            image = await asyncio.to_thread(
                generate,
                bundle=bundle,
                credentials=creds,
                payload=payload,
                impersonate=settings.submit_impersonate,
                poll_timeout_seconds=remaining,
                proxy_url=proxy_url,
                trace=trace,
            )
            url = await store_image(
                job_id=job_id,
                image=image,
                cfg=cfg,
                cipher=resources.cipher,
                media_dir=media_dir,
                redis=resources.redis,
            )
            upstream_urls = adobe_result_urls(image)
            async with resources.request_sessions() as session:
                job = await session.get(GenerationJob, job_id)
                acc = (
                    await session.get(UpstreamAccount, account.id) if account is not None else None
                )
                if job is None or job.status != "running":
                    await session.commit()
                    return
                params = dict(job.params or {})
                params["upstream_urls"] = upstream_urls
                job.params = params
                await lifecycle.mark_succeeded(session, job, result_urls=[url])
                if acc is not None:
                    await account_pool.bump_daily(session, acc)
                    await account_pool.clear_408_streak(resources.redis, acc.id)
                await account_pool.record_egress_outcome(
                    resources.redis, egress_id=egress_id, lane=lane, outcome="ok"
                )
                await _log(
                    session,
                    job_id=job_id,
                    account_hash=account.fingerprint if account else "",
                    egress_id=egress_id,
                    arp_id=bundle.id if bundle else None,
                    status_code=200,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    retry_count=attempt,
                    outcome="ok",
                    trace=trace.export(),
                )
                await session.commit()
            return
        except AdobeGenerateError as exc:
            last_error = exc
            async with resources.request_sessions() as session:
                if account is not None:
                    acc = await session.get(UpstreamAccount, account.id)
                    if acc is not None and exc.code == "arp_rejected" and not web_lane:
                        await account_pool.park_after_408(
                            session,
                            resources.redis,
                            acc,
                            first=cfg.account_park_seconds,
                            second=cfg.account_park_second_seconds,
                            rest=cfg.account_park_disable_seconds,
                            disable_after=cfg.account_park_disable_after,
                            observe_seconds=cfg.account_408_observe_seconds,
                        )
                        await account_pool.record_408_hit(
                            resources.redis,
                            window_seconds=cfg.arp_bad_window_seconds,
                            threshold=cfg.arp_bad_window_count,
                            pause_seconds=cfg.arp_bad_window_pause_seconds,
                        )
                    if acc is not None and exc.code == "auth_failed":
                        acc.access_token_enc = None
                        acc.token_expires_at = None
                if bundle is not None and exc.code in {
                    "arp_rejected",
                    "identity_rejected",
                    "proxy_tls",
                }:
                    await _arp_store(app, cfg.arp_ttl_seconds, cfg.arp_max_uses).invalidate(
                        bundle.id
                    )
                await account_pool.record_egress_outcome(
                    resources.redis,
                    egress_id=egress_id,
                    lane=lane,
                    outcome=exc.code,
                )
                if exc.code not in _CAPACITY_CODES:
                    job_row = await session.get(GenerationJob, job_id)
                    if job_row is not None:
                        job_row.retry_count = max(int(job_row.retry_count or 0), attempt + 1)
                    await _log(
                        session,
                        job_id=job_id,
                        account_hash=account.fingerprint if account else "",
                        egress_id=egress_id,
                        arp_id=bundle.id if bundle else None,
                        status_code=exc.status_code,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        retry_count=attempt,
                        outcome=exc.code,
                        trace=trace.export(),
                    )
                await session.commit()
            if exc.code in _CAPACITY_CODES:
                if await _park_for_capacity(
                    app,
                    job_id=job_id,
                    code=exc.code,
                    timeout_seconds=cfg.generation_timeout_seconds,
                ):
                    return
                last_error = _TIMEOUT_ERROR
                break
            if not web_lane and exc.code == "arp_rejected" and (
                attempt >= 2
                or (
                    cfg.arp_bad_window_pause_seconds > 0
                    and await account_pool.bad_window_active(resources.redis)
                )
            ):
                break
            if exc.code not in _RETRYABLE and exc.code not in {"auth_failed"}:
                break
            if exc.code in {"rate_limited", "content_rejected"}:
                sticky_account_id = None
                sticky_credentials = None
            delay = cfg.retry_backoff_base_seconds * (2**attempt)
            if web_lane and exc.code == "arp_rejected":
                delay = cfg.alpha_retry_delay_seconds
            elif exc.code in {"arp_rejected", "rate_limited"}:
                delay = max(delay, 2.0)
            left = cfg.generation_timeout_seconds - _generation_age_seconds(job)
            if left <= 0:
                last_error = _TIMEOUT_ERROR
                break
            await asyncio.sleep(min(delay, left))
        except AppError as exc:
            last_error = AdobeGenerateError(exc.code, exc.http_status, exc.message)
            async with resources.request_sessions() as session:
                job_row = await session.get(GenerationJob, job_id)
                if job_row is not None:
                    job_row.retry_count = max(int(job_row.retry_count or 0), attempt + 1)
                await _log(
                    session,
                    job_id=job_id,
                    account_hash=account.fingerprint if account else "",
                    egress_id=egress_id,
                    arp_id=bundle.id if bundle else None,
                    status_code=exc.http_status,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    retry_count=attempt,
                    outcome=exc.code,
                    trace=trace.export(),
                )
                await session.commit()
            break
        except Exception as exc:
            dropped = is_transient_connect(exc)
            last_error = AdobeGenerateError(
                "proxy_tls" if dropped else "upstream_error",
                502 if dropped else 500,
                "upstream connection dropped" if dropped else "internal worker error",
            )
            logger.exception("job %s crashed", job_id)
            async with resources.request_sessions() as session:
                if dropped and bundle is not None:
                    await _arp_store(app, cfg.arp_ttl_seconds, cfg.arp_max_uses).invalidate(
                        bundle.id
                    )
                await _log(
                    session,
                    job_id=job_id,
                    account_hash=account.fingerprint if account else "",
                    egress_id=egress_id,
                    arp_id=bundle.id if bundle else None,
                    status_code=last_error.status_code,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    retry_count=attempt,
                    outcome=last_error.code,
                    trace=trace.export()
                    + [{"step": "crash", "error": redact(str(exc)[:400])}],
                )
                await session.commit()
            if not dropped:
                break
            await asyncio.sleep(cfg.retry_backoff_base_seconds * (2**attempt))
        finally:
            if account is not None:
                await account_pool.release(resources.redis, account.id)

    async with resources.request_sessions() as session:
        job = await session.get(GenerationJob, job_id)
        if job is None or job.status not in {"queued", "running"}:
            await session.commit()
            return
        if job.status == "queued":
            await lifecycle.mark_running(session, job)
        rejected = last_error.code == "content_rejected"
        await lifecycle.mark_failed(
            session,
            job,
            code=last_error.code,
            message=public_fail_message(
                last_error.code, last_error.status_code, str(last_error)
            ),
            rejected=rejected,
        )
        await session.commit()


async def worker_loop(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    if not settings.gen_worker_enabled:
        logger.info("generation worker disabled")
        return
    redis = app.state.resources.redis
    try:
        await asyncio.wait_for(redis.ping(), 0.4)
    except Exception:
        logger.warning("generation worker idle: redis unavailable")
        return
    logger.info("generation worker listening")
    try:
        await sweep_timed_out_jobs(app)
        await reclaim_open_jobs(app)
    except Exception:
        logger.warning("generation job reclaim skipped", exc_info=True)
    cfg = await _config_store(app).load()
    fanout = max(1, min(int(cfg.worker_concurrency), 200))
    sem = asyncio.Semaphore(fanout)
    inflight: set[asyncio.Task[None]] = set()

    async def _run(job_id: str) -> None:
        async with sem:
            try:
                await process_job(app, job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker dropped job")

    while True:
        try:
            await promote_delayed(redis)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("delayed queue promote failed")
        try:
            item = await redis.brpop(QUEUE_KEY, timeout=2)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)
            continue
        if not item:
            continue
        job_id = item[1] if isinstance(item[1], str) else item[1].decode()
        task = asyncio.create_task(_run(job_id))
        inflight.add(task)
        task.add_done_callback(inflight.discard)
