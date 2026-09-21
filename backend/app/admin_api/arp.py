import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.arp.models import ArpInvalidateRequest, ArpPublishRequest
from app.arp.store import ArpStore
from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import Resources

router = APIRouter(prefix="/admin-api/adobe/arp", tags=["internal-arp"])


def _store(request: Request) -> ArpStore:
    settings: Settings = request.app.state.settings
    resources: Resources = request.app.state.resources
    return ArpStore(
        resources.redis,
        key_prefix=settings.arp_key_prefix,
        ttl_seconds=settings.arp_ttl_seconds,
        max_uses=settings.arp_max_uses,
    )


async def require_internal_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    settings: Settings = request.app.state.settings
    if token is None or not hmac.compare_digest(token, settings.arp_internal_token):
        raise AppError(
            code="internal_auth_failed",
            message="invalid internal credentials",
            http_status=401,
        )


@router.post("")
async def publish(
    payload: ArpPublishRequest,
    request: Request,
    _auth: Annotated[None, Depends(require_internal_token)],
) -> dict[str, object]:
    ids = await _store(request).publish(payload.tokens)
    return {"ok": True, "published": len(ids), "ids": ids}


@router.get("")
async def health(
    request: Request,
    _auth: Annotated[None, Depends(require_internal_token)],
) -> dict[str, object]:
    result = await _store(request).health()
    return {"ok": True, **result}


@router.post("/invalidate")
async def invalidate(
    payload: ArpInvalidateRequest,
    request: Request,
    _auth: Annotated[None, Depends(require_internal_token)],
) -> dict[str, object]:
    removed = await _store(request).invalidate(payload.id)
    return {"ok": True, "removed": removed}
