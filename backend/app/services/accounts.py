"""Upstream account pool: import cookies, lease, park. AT is minted per request, never stored."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.cookie_loader import CookieRecord, load_cookie_file
from app.adobe.auth import AdobeAuthError, AdobeCredentials, exchange_cookie_for_clio
from app.adobe.client import AdobeGenerateError
from app.adobe.credits import AdobeCreditsError, fetch_credits_balance
from app.adobe.ff_ios import (
    FF_IOS_UA,
    FFIOSAuthError,
)
from app.adobe.ff_ios import (
    mint as mint_ff_ios,
)
from app.adobe.ff_ios import (
    refresh as refresh_ff_ios,
)
from app.adobe.trace import UpstreamTrace
from app.core.crypto import FieldCipher
from app.core.errors import AppError
from app.models.account import UpstreamAccount

LEASE_PREFIX = "firefly:acct:lease:"
LEASE_SECONDS = 180
PARK_SECONDS = 45
STREAK_PREFIX = "firefly:acct:408streak:"
OBSERVE_PREFIX = "firefly:acct:observe:"
WINDOW_HITS_KEY = "firefly:arp:408hits"
WINDOW_PAUSE_KEY = "firefly:arp:badwindow"
EGRESS_OUTCOME_PREFIX = "firefly:arp:egress:"
STREAK_TTL = 86_400
ACCOUNT_LEASE_CAP = 16


def park_seconds_for_streak(
    streak: int,
    *,
    first: int,
    second: int,
    rest: int,
    disable_after: int,
) -> int:
    """Escalate 408 cool-down. Never mark the account dead."""
    n = max(1, int(streak))
    if n <= 1:
        return max(30, int(first))
    if n == 2:
        return max(30, int(second))
    if n >= max(2, int(disable_after)):
        return max(30, int(rest))
    return max(30, int(second))


async def streak_of(redis: Redis, account_id: int) -> int:
    try:
        raw = await redis.get(f"{STREAK_PREFIX}{account_id}")
    except Exception:
        return 0
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


async def mark_observing(redis: Redis, account_id: int, seconds: int) -> None:
    ttl = max(0, int(seconds))
    if ttl <= 0:
        return
    try:
        await redis.set(f"{OBSERVE_PREFIX}{account_id}", "1", ex=ttl)
    except Exception:
        return


async def is_observing(redis: Redis, account_id: int) -> bool:
    try:
        return bool(await redis.get(f"{OBSERVE_PREFIX}{account_id}"))
    except Exception:
        return False


async def bad_window_active(redis: Redis) -> bool:
    try:
        return bool(await redis.get(WINDOW_PAUSE_KEY))
    except Exception:
        return False


async def record_408_hit(
    redis: Redis, *, window_seconds: int, threshold: int, pause_seconds: int
) -> bool:
    """Count recent 408s. Trip a pause so workers stop swapping accounts."""
    if pause_seconds <= 0:
        return False
    try:
        hits = int(await redis.incr(WINDOW_HITS_KEY))
        if hits == 1:
            await redis.expire(WINDOW_HITS_KEY, max(10, int(window_seconds)))
        if hits >= max(2, int(threshold)):
            await redis.set(WINDOW_PAUSE_KEY, "1", ex=max(1, int(pause_seconds)))
            return True
        return bool(await redis.get(WINDOW_PAUSE_KEY))
    except Exception:
        return False


async def record_egress_outcome(
    redis: Redis, *, egress_id: str, lane: str, outcome: str
) -> None:
    """Accumulate evidence without penalising an exit from a single sample."""
    if not egress_id:
        return
    key = f"{EGRESS_OUTCOME_PREFIX}{egress_id}"
    field = f"{lane}:{outcome}"[:96]
    try:
        await cast(Awaitable[int], redis.hincrby(key, field, 1))
        await redis.expire(key, STREAK_TTL)
    except Exception:
        return


def mask_identity(raw: str) -> str:
    value = raw.strip()
    if "@" in value:
        user, _, domain = value.partition("@")
        head = user[:2] if len(user) >= 2 else user[:1]
        return f"{head}***@{domain}"
    if len(value) <= 6:
        return "***"
    return f"{value[:4]}***{value[-2:]}"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def view_of(row: UpstreamAccount, cipher: FieldCipher | None = None) -> dict[str, Any]:
    cookie = ""
    if cipher is not None:
        try:
            cookie = cookie_of(row, cipher)
        except Exception:
            cookie = ""
    return {
        "id": row.id,
        "email_masked": row.email_masked,
        "plan_cap": row.plan_cap,
        "group": row.group,
        "credential_status": row.credential_status,
        "credential_error": row.credential_error,
        "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
        "ios_device_ready": bool(row.ios_device_token_enc and row.ios_device_id_enc),
        "cookie": cookie,
        "credits_remaining": row.credits_remaining,
        "credits_total": row.credits_total,
        "status": row.status,
        "parked_until": row.parked_until.isoformat() if row.parked_until else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "updated_at": row.updated_at.isoformat(),
    }


async def import_records(
    session: AsyncSession,
    cipher: FieldCipher,
    records: list[CookieRecord],
    *,
    group: str = "alpha",
) -> dict[str, int | list[int]]:
    created = 0
    skipped = 0
    ids: list[int] = []
    for rec in records:
        existing = await session.scalar(
            select(UpstreamAccount).where(UpstreamAccount.fingerprint == rec.fingerprint)
        )
        masked = mask_identity(rec.identity) if rec.identity else f"acct-{rec.fingerprint[-8:]}"
        if existing is not None:
            existing.cookie_enc = cipher.encrypt(rec.value)
            existing.access_token_enc = None
            existing.token_expires_at = None
            existing.ios_device_token_enc = None
            existing.ios_device_id_enc = None
            existing.credential_status = "unverified"
            existing.credential_error = None
            existing.last_verified_at = None
            if rec.identity:
                existing.email_masked = masked
            ids.append(int(existing.id))
            skipped += 1
            continue
        row = UpstreamAccount(
            fingerprint=rec.fingerprint,
            email_masked=masked,
            cookie_enc=cipher.encrypt(rec.value),
            access_token_enc=None,
            token_expires_at=None,
            plan_cap="UNKNOWN",
            group=group,
            status="active",
            credential_status="unverified",
        )
        session.add(row)
        await session.flush()
        ids.append(int(row.id))
        created += 1
    return {"created": created, "skipped": skipped, "total": len(records), "ids": ids}


async def import_file(
    session: AsyncSession, cipher: FieldCipher, path: str, *, group: str = "alpha"
) -> dict[str, int | list[int]]:
    return await import_records(session, cipher, load_cookie_file(path), group=group)


def cookie_of(row: UpstreamAccount, cipher: FieldCipher) -> str:
    return cipher.decrypt(row.cookie_enc)


async def checkout(
    session: AsyncSession,
    redis: Redis,
    *,
    daily_limit: int,
    max_leases: int = 1,
    advanced: bool = False,
    model: str = "",
    group: str | None = None,
    account_id: int | None = None,
    block_models: set[str] | frozenset[str] | None = None,
    block_after: int = 1,
    force_single: bool = False,
) -> UpstreamAccount | None:
    now = datetime.now(UTC)
    today = _today()
    cap = "HARD"
    slots = max(1, min(int(max_leases), ACCOUNT_LEASE_CAP))
    blocked = {item.strip().lower() for item in (block_models or set()) if item.strip()}
    model_key = model.strip().lower()
    rows = list(
        await session.scalars(
            select(UpstreamAccount)
            .where(
                UpstreamAccount.status.in_(("active", "parked")),
                UpstreamAccount.plan_cap == cap,
                *(
                    (UpstreamAccount.group == group,)
                    if group in {"normal", "alpha"}
                    else ()
                ),
                *((UpstreamAccount.id == account_id,) if account_id is not None else ()),
            )
            .order_by(UpstreamAccount.last_used_at.is_(None).desc(), UpstreamAccount.last_used_at)
        )
    )
    for row in rows:
        if row.status == "parked":
            if row.parked_until is None or row.parked_until > now:
                continue
            row.status = "active"
            row.parked_until = None
        if row.daily_reset_on != today:
            row.daily_used = 0
            row.daily_reset_on = today
        if row.daily_used >= daily_limit:
            continue
        streak = await streak_of(redis, row.id)
        if blocked and model_key in blocked and streak >= max(1, int(block_after)):
            continue
        observing = await is_observing(redis, row.id)
        use_slots = 1 if (force_single or observing) else slots
        key = f"{LEASE_PREFIX}{row.id}:n"
        held = int(await redis.incr(key))
        await redis.expire(key, LEASE_SECONDS)
        if held > use_slots:
            await redis.decr(key)
            continue
        row.last_used_at = now
        await session.flush()
        return row
    _ = advanced
    return None


async def release_expired_parks(session: AsyncSession) -> int:
    """Unpark timed cool-downs that have elapsed. parked_until IS NULL stays parked."""
    now = datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(UpstreamAccount).where(
                UpstreamAccount.status == "parked",
                UpstreamAccount.parked_until.is_not(None),
                UpstreamAccount.parked_until <= now,
            )
        )
    )
    for row in rows:
        row.status = "active"
        row.parked_until = None
    if rows:
        await session.flush()
    return len(rows)


async def watchdog_loop(resources: Any, *, interval_seconds: float = 15) -> None:
    """Wake parked accounts when parked_until elapses. Does not touch open-ended parks."""
    log = logging.getLogger(__name__)
    while True:
        try:
            async with resources.request_sessions() as session:
                n = await release_expired_parks(session)
                await session.commit()
            if n:
                log.info("account watchdog restored %s cooled accounts", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("account park watchdog skipped", exc_info=True)
        await asyncio.sleep(max(5.0, float(interval_seconds)))


async def release(redis: Redis, account_id: int) -> None:
    key = f"{LEASE_PREFIX}{account_id}:n"
    held = int(await redis.decr(key))
    if held <= 0:
        await redis.delete(key)


async def park(session: AsyncSession, row: UpstreamAccount, *, seconds: int = PARK_SECONDS) -> None:
    row.status = "parked"
    row.parked_until = datetime.now(UTC) + timedelta(seconds=max(30, int(seconds)))
    row.last_error = "parked after upstream 408"
    await session.flush()


async def park_after_408(
    session: AsyncSession,
    redis: Redis,
    row: UpstreamAccount,
    *,
    first: int,
    second: int,
    rest: int,
    disable_after: int,
    observe_seconds: int = 0,
) -> int:
    """Timed park with streak. Auto-unpark via parked_until. Never dead."""
    key = f"{STREAK_PREFIX}{row.id}"
    streak = 1
    try:
        streak = int(await redis.incr(key))
        await redis.expire(key, STREAK_TTL)
    except Exception:
        streak = 1
    seconds = park_seconds_for_streak(
        streak, first=first, second=second, rest=rest, disable_after=disable_after
    )
    await park(session, row, seconds=seconds)
    row.last_error = f"408 x{streak}, cool {seconds}s"
    extra = max(0, int(observe_seconds))
    if extra:
        await mark_observing(redis, row.id, seconds + extra)
    await session.flush()
    return seconds


async def clear_408_streak(redis: Redis, account_id: int) -> None:
    try:
        await redis.delete(f"{STREAK_PREFIX}{account_id}")
        await redis.delete(f"{OBSERVE_PREFIX}{account_id}")
    except Exception:
        return


async def mark_dead(session: AsyncSession, row: UpstreamAccount, reason: str) -> None:
    row.status = "dead"
    row.last_error = reason[:255]
    row.credential_status = "invalid"
    row.credential_error = reason[:255]
    await session.flush()


async def bump_daily(session: AsyncSession, row: UpstreamAccount) -> None:
    today = _today()
    if row.daily_reset_on != today:
        row.daily_used = 0
        row.daily_reset_on = today
    row.daily_used += 1
    await session.flush()


async def credentials_from_cookie(
    session: AsyncSession,
    row: UpstreamAccount,
    cipher: FieldCipher,
    *,
    proxy_url: str,
    user_agent: str,
    impersonate: str,
    trace: UpstreamTrace | None = None,
) -> AdobeCredentials:
    """Mint a clio AT from the stored Cookie for this request. Never persist the AT."""
    try:
        creds = await asyncio.to_thread(
            exchange_cookie_for_clio,
            cookie=cookie_of(row, cipher),
            proxy_url=proxy_url,
            user_agent=user_agent,
            impersonate=impersonate,
            trace=trace,
        )
    except AdobeAuthError as exc:
        await _raise_credential_error(session, row, exc)
    row.access_token_enc = None
    row.token_expires_at = None
    if creds.rotated_cookies:
        row.cookie_enc = cipher.encrypt(
            _merge_cookie(cookie_of(row, cipher), creds.rotated_cookies)
        )
    row.credential_status = "ready"
    row.credential_error = None
    row.last_verified_at = datetime.now(UTC)
    if creds.account_id and (not row.email_masked or row.email_masked.startswith("acct-")):
        row.email_masked = mask_identity(creds.account_id)
    await session.flush()
    return creds


cached_credentials = credentials_from_cookie


async def credentials_for_account(
    session: AsyncSession,
    row: UpstreamAccount,
    cipher: FieldCipher,
    *,
    proxy_url: str,
    user_agent: str,
    impersonate: str,
    identity: str = "web",
    trace: UpstreamTrace | None = None,
) -> AdobeCredentials:
    if identity == "mobile":
        return await _credentials_for_ff_ios(
            session,
            row,
            cipher,
            proxy_url=proxy_url,
            impersonate=impersonate,
        )
    return await credentials_from_cookie(
        session,
        row,
        cipher,
        proxy_url=proxy_url,
        user_agent=user_agent,
        impersonate=impersonate,
        trace=trace,
    )


async def _credentials_for_ff_ios(
    session: AsyncSession,
    row: UpstreamAccount,
    cipher: FieldCipher,
    *,
    proxy_url: str,
    impersonate: str,
) -> AdobeCredentials:
    device_token = (
        cipher.decrypt(row.ios_device_token_enc) if row.ios_device_token_enc else ""
    )
    device_id = cipher.decrypt(row.ios_device_id_enc) if row.ios_device_id_enc else ""
    try:
        if device_token and device_id:
            native = await asyncio.to_thread(
                refresh_ff_ios,
                device_token=device_token,
                device_id=device_id,
                proxy_url=proxy_url,
                impersonate=impersonate,
            )
        else:
            native = await asyncio.to_thread(
                mint_ff_ios,
                cookie=cookie_of(row, cipher),
                seed=row.fingerprint,
                proxy_url=proxy_url,
                impersonate=impersonate,
            )
    except FFIOSAuthError as exc:
        await _raise_credential_error(session, row, exc)
    row.ios_device_token_enc = cipher.encrypt(native.device_token)
    row.ios_device_id_enc = cipher.encrypt(native.device_id)
    row.access_token_enc = None
    row.token_expires_at = None
    row.credential_status = "ready"
    row.credential_error = None
    row.last_verified_at = datetime.now(UTC)
    if native.account_id and (not row.email_masked or row.email_masked.startswith("acct-")):
        row.email_masked = mask_identity(native.account_id)
    await session.flush()
    return AdobeCredentials(
        access_token=native.access_token,
        account_id=native.account_id,
        expires_in=native.expires_in,
        token_kind="ff_ios",
    )


async def _raise_credential_error(
    session: AsyncSession,
    row: UpstreamAccount,
    exc: AdobeAuthError | FFIOSAuthError,
) -> NoReturn:
    code = exc.code
    if code == "auth_invalid":
        await mark_dead(session, row, "credential_invalid")
        raise AdobeGenerateError("auth_failed", exc.status_code or 401, "账号鉴权失败") from exc
    row.credential_status = "transient"
    row.credential_error = code[:255]
    row.last_error = code[:255]
    await session.flush()
    mapped = {
        "proxy_tls": ("proxy_tls", 502),
        "rate_limited": ("rate_limited", 429),
        "identity_rejected": ("identity_rejected", 403),
    }.get(code, ("upstream_error", exc.status_code or 502))
    raise AdobeGenerateError(mapped[0], mapped[1], "上游凭据服务暂时不可用") from exc


def _merge_cookie(cookie: str, replacements: dict[str, str]) -> str:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in cookie.split(";"):
        name, sep, value = item.strip().partition("=")
        if not sep or not name:
            continue
        key = name.lower()
        pairs.append((name, replacements.get(key, value)))
        seen.add(key)
    for name, value in replacements.items():
        if name.lower() not in seen and value:
            pairs.append((name, value))
    return "; ".join(f"{name}={value}" for name, value in pairs)


async def replace_cookie(
    session: AsyncSession,
    row: UpstreamAccount,
    cipher: FieldCipher,
    rec: CookieRecord,
) -> UpstreamAccount:
    clash = await session.scalar(
        select(UpstreamAccount).where(
            UpstreamAccount.fingerprint == rec.fingerprint,
            UpstreamAccount.id != row.id,
        )
    )
    if clash is not None:
        raise AppError(code="cookie_duplicate", message="该 Cookie 已在号池中", http_status=409)
    row.cookie_enc = cipher.encrypt(rec.value)
    row.fingerprint = rec.fingerprint
    row.access_token_enc = None
    row.token_expires_at = None
    row.credential_status = "unverified"
    row.credential_error = None
    row.last_verified_at = None
    row.ios_device_token_enc = None
    row.ios_device_id_enc = None
    if rec.identity:
        row.email_masked = mask_identity(rec.identity)
    await session.flush()
    return row


async def refresh_credits(
    session: AsyncSession,
    row: UpstreamAccount,
    cipher: FieldCipher,
    *,
    proxy_url: str,
    user_agent: str,
    impersonate: str,
) -> UpstreamAccount:
    """Mint AT from Cookie, query Firefly credits, store the snapshot. Never persist AT."""
    creds = await credentials_for_account(
        session,
        row,
        cipher,
        proxy_url=proxy_url,
        user_agent=user_agent,
        impersonate=impersonate,
        identity="web",
    )
    try:
        balance = await asyncio.to_thread(
            fetch_credits_balance,
            access_token=creds.access_token,
            account_id=creds.account_id,
            proxy_url=proxy_url,
            user_agent=FF_IOS_UA if creds.token_kind == "ff_ios" else user_agent,
            impersonate=impersonate,
            api_key="FF-iOS" if creds.token_kind == "ff_ios" else "SunbreakWebUI1",
        )
    except AdobeAuthError as exc:
        await _raise_credential_error(session, row, exc)
    except AdobeCreditsError as exc:
        row.last_error = "credits_query_failed"
        await session.flush()
        raise AppError(
            code="credits_query_failed", message="上游额度查询失败", http_status=502
        ) from exc
    row.credits_remaining = balance.remaining
    row.credits_total = balance.total
    if balance.plan_cap in {"HARD", "FREE"}:
        row.plan_cap = balance.plan_cap
    row.last_error = None
    row.credential_status = "ready"
    row.credential_error = None
    row.last_verified_at = datetime.now(UTC)
    if row.status == "dead":
        row.status = "active"
        row.parked_until = None
    await session.flush()
    return row


async def set_status(session: AsyncSession, account_id: int, status: str) -> UpstreamAccount:
    row = await session.get(UpstreamAccount, account_id)
    if row is None:
        raise AppError(code="account_not_found", message="账号不存在", http_status=404)
    if status not in {"active", "parked", "ratelimited", "dead"}:
        raise AppError(code="invalid_status", message="状态无效", http_status=422)
    row.status = status
    if status == "active":
        row.parked_until = None
    await session.flush()
    return row


async def set_group(
    session: AsyncSession, row: UpstreamAccount, group: str
) -> UpstreamAccount:
    if group not in {"normal", "alpha"}:
        raise AppError(code="invalid_group", message="账号分组无效", http_status=422)
    row.group = group
    await session.flush()
    return row


async def counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(UpstreamAccount.status, func.count()).group_by(UpstreamAccount.status)
    )
    data = {str(status): int(n) for status, n in rows}
    total = sum(data.values())
    hard = int(
        await session.scalar(
            select(func.count()).where(UpstreamAccount.plan_cap == "HARD")
        )
        or 0
    )
    credits_available = int(
        await session.scalar(
            select(func.coalesce(func.sum(UpstreamAccount.credits_remaining), 0)).where(
                UpstreamAccount.status == "active"
            )
        )
        or 0
    )
    return {
        "total": total,
        "active": data.get("active", 0),
        "parked": data.get("parked", 0),
        "hard": hard,
        "credits_available": credits_available,
    }
