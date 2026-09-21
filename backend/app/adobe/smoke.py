from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from uuid import uuid4

from app.accounts.cookie_loader import load_cookie_file
from app.adobe.auth import exchange_cookie_for_clio
from app.adobe.client import AdobeGenerateError, generate_smoke_image
from app.arp.store import ArpStore
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.resources import create_resources

logger = logging.getLogger("adobe_smoke")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.test_cookie_file:
        raise RuntimeError("TEST_COOKIE_FILE is required")

    cookies = load_cookie_file(settings.test_cookie_file)
    account_index = int(os.getenv("TEST_COOKIE_INDEX", "0"))
    if not 0 <= account_index < len(cookies):
        raise RuntimeError("TEST_COOKIE_INDEX is out of range")
    cookie = cookies[account_index]

    resources = create_resources(settings)
    store = ArpStore(
        resources.redis,
        key_prefix=settings.arp_key_prefix,
        ttl_seconds=settings.arp_ttl_seconds,
        max_uses=settings.arp_max_uses,
    )
    try:
        bundle = await store.checkout()
        if bundle is None:
            raise RuntimeError("ARP pool is empty; mint a fresh bundle first")
        logger.info(
            "starting smoke generation arp=%s egress=%s account=%s",
            bundle.id,
            bundle.egress_id,
            cookie.fingerprint,
        )
        credentials = await asyncio.to_thread(
            exchange_cookie_for_clio,
            cookie=cookie.value,
            proxy_url=bundle.proxy_url.get_secret_value(),
            user_agent=bundle.ua,
            impersonate=settings.submit_impersonate,
        )
        prompt = os.getenv(
            "SMOKE_PROMPT",
            "a single red apple on a clean white studio background, photorealistic",
        )
        try:
            image = await asyncio.to_thread(
                generate_smoke_image,
                bundle=bundle,
                credentials=credentials,
                prompt=prompt,
                impersonate=settings.submit_impersonate,
                poll_timeout_seconds=settings.poll_timeout_seconds,
            )
        except AdobeGenerateError as exc:
            if exc.code == "arp_rejected":
                await store.invalidate(bundle.id)
            raise

        output_dir = PROJECT_ROOT / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        extension = ".jpg" if "jpeg" in image.content_type else ".png"
        output_path = output_dir / f"smoke-{uuid4().hex}{extension}"
        output_path.write_bytes(image.content)
        logger.info(
            "smoke generation succeeded bytes=%d content_type=%s output=%s",
            len(image.content),
            image.content_type,
            output_path,
        )
    finally:
        await resources.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
