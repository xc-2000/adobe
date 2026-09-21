import base64
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from app.core.config import Settings
from app.main import create_app


def publish_payload() -> dict[str, object]:
    token = base64.b64encode(
        json.dumps({"sid": "sid", "ark": "ark", "bfp": "bfp", "ftr": "full_forter"}).encode()
    ).decode()
    return {
        "tokens": [
            {
                "t": token,
                "proxy_url": "socks5h://user:password@proxy.example:1080",
                "egress_id": "residential-1",
                "ua": "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
                "ch": {
                    "platform": "Windows",
                    "platformVersion": "15.0.0",
                    "fullVersionList": [
                        {"brand": "Google Chrome", "version": "148.0.0.0"}
                    ],
                    "architecture": "x86",
                    "bitness": "64",
                },
            }
        ]
    }


def test_admin_arp_rejects_missing_internal_token() -> None:
    with TestClient(create_app(Settings())) as client:
        response = client.get("/admin-api/adobe/arp")

    assert response.status_code == 401
    assert response.json()["code"] == "internal_auth_failed"


def test_admin_arp_publish_and_health() -> None:
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        redis.ping()
    except Exception:
        pytest.skip("Redis is not available")

    prefix = f"test:admin-arp:{uuid4().hex}"
    settings = Settings(
        redis_url="redis://127.0.0.1:6379/15",
        arp_key_prefix=prefix,
        arp_internal_token="test-internal-token",
    )
    headers = {"X-Internal-Token": "test-internal-token"}
    try:
        with TestClient(create_app(settings)) as client:
            published = client.post(
                "/admin-api/adobe/arp",
                headers=headers,
                json=publish_payload(),
            )
            health = client.get("/admin-api/adobe/arp", headers=headers)
    finally:
        keys = list(redis.scan_iter(f"{prefix}:*"))
        if keys:
            redis.delete(*keys)
        redis.close()

    assert published.status_code == 200
    assert published.json()["published"] == 1
    assert health.status_code == 200
    assert health.json()["ready"] == 1
    assert "proxy_url" not in health.text
