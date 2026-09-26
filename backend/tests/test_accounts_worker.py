from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.adobe.auth import AdobeAuthError, AdobeCredentials
from app.adobe.client import AdobeGenerateError, GeneratedImage
from app.adobe.ff_ios import FFIOSCredentials
from app.arp.models import ArpBundle
from app.core.config import Settings
from app.main import create_app
from app.models import GenerationJob
from app.models.account import UpstreamAccount
from app.services.config.proxy import ProxySession
from app.services.generation.worker import process_job, sweep_timed_out_jobs


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
        redis_url="redis://127.0.0.1:1/0",
        field_encryption_key=Fernet.generate_key().decode(),
        app_secret_key="test-secret-key-with-thirty-two-bytes!!",
        admin_username="root",
        admin_password="root-password-1",
        config_cache_seconds=0,
        gen_worker_enabled=False,
        media_dir=str(tmp_path / "media"),
    )
    with TestClient(create_app(settings)) as c:
        yield c


def _admin(client: TestClient) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"username": "root", "password": "root-password-1"})
    return {"Authorization": f"Bearer {res.json()['token']}"}


async def _mark_imported_hard(client: TestClient) -> None:
    resources = client.app.state.resources
    async with resources.sessions() as session:
        await session.execute(
            update(UpstreamAccount).values(plan_cap="HARD", credential_status="ready")
        )
        await session.commit()


def _enable_dual_identity(client: TestClient, admin: dict[str, str]) -> None:
    cfg = client.get("/api/admin/config", headers=admin).json()
    cfg["adobe_dual_identity_enabled"] = True
    cfg["retry_max_attempts"] = 3
    cfg["retry_backoff_base_seconds"] = 0.05
    cfg["alpha_retry_max_attempts"] = 3
    cfg["alpha_retry_delay_seconds"] = 0.2
    cfg["proxy"].update(
        host="proxy.invalid",
        username="dual-test",
        password="dual-test-password",
        country="SG",
    )
    saved = client.put("/api/admin/config", headers=admin, json=cfg)
    assert saved.status_code == 200, saved.text


def test_import_cookies_are_shown_to_admin_not_in_logs(client: TestClient) -> None:
    admin = _admin(client)
    raw = "ims_sid=secretvalue123; other=abc123"
    res = client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": raw, "group": "alpha"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["created"] == 1
    assert res.json()["verifying"] == 1
    listed = client.get("/api/admin/accounts", headers=admin).json()
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["cookie"] == raw
    assert item["plan_cap"] == "UNKNOWN"
    assert item["credential_status"] == "unverified"
    assert "access_token" not in str(item)
    stats = client.get("/api/admin/stats/overview", headers=admin).json()
    assert stats["accounts_active"] == 1
    assert "credits_total" in stats
    assert "jobs_total" in stats
    assert "revenue_total" in stats
    logs = client.get("/api/admin/logs", headers=admin).json()
    assert logs["total"] == 0
    assert "secretvalue123" not in str(logs)

    exported = client.get("/api/admin/accounts/export", headers=admin).json()
    assert exported["count"] == 1
    assert raw in exported["text"]
    assert "----" not in exported["text"]


def test_import_schedules_background_verify(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    scheduled: list[list[int]] = []

    def fake_schedule(
        _resources: object, _store: object, _settings: object, ids: list[int]
    ) -> None:
        scheduled.append(list(ids))

    monkeypatch.setattr("app.api.admin_accounts._schedule_import_verify", fake_schedule)
    res = client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] == 1
    assert body["skipped"] == 0
    assert body["verifying"] == 1
    assert scheduled and scheduled[0]
    item = client.get("/api/admin/accounts", headers=admin).json()["items"][0]
    assert item["plan_cap"] == "UNKNOWN"
    assert item["credential_status"] == "unverified"
    assert item["group"] == "alpha"


@pytest.mark.asyncio
async def test_worker_settles_hold_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123", "group": "alpha"},
    )
    await _mark_imported_hard(client)
    user = client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "carol",
            "email": "carol@example.com",
            "password": "carol-pass-1",
            "initial_balance": 1000,
        },
    ).json()
    token = client.post(
        "/api/auth/login", json={"username": "carol", "password": "carol-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana-pro",
            "prompt": "a red apple",
            "resolution": "1K",
            "idempotency_key": "worker-success-01",
        },
    ).json()
    assert job["status"] == "queued"
    assert client.get("/api/auth/me", headers=headers).json()["balance"] == 990

    bundle = ArpBundle.model_validate(
        {
            "id": "arp_test",
            "token": "dGVzdA==",
            "proxy_url": "http://127.0.0.1:9",
            "egress_id": "sid-test",
            "ua": "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
            "ch": {},
            "headers": {},
            "minted_at": 1,
            "expires_at": 9_999_999_999,
            "uses": 1,
            "max_uses": 20,
        }
    )
    # token must be valid ARP json base64 — override with a dummy store instead.

    class FakeArp:
        async def health(self) -> dict[str, object]:
            return {"ready": 8, "present": True}

        async def checkout(self) -> ArpBundle:
            return bundle

        async def invalidate(self, _bundle_id: str) -> bool:
            return True

    listed = client.get("/api/admin/accounts", headers=admin).json()["items"][0]
    account_id = int(listed["id"])

    async def fake_checkout(session: object, *_a: object, **_k: object) -> object:
        from app.models.account import UpstreamAccount

        row = await session.get(UpstreamAccount, account_id)  # type: ignore[union-attr]
        assert row is not None
        return row

    async def fake_creds(*_a: object, **_k: object) -> AdobeCredentials:
        return AdobeCredentials(access_token="tok", account_id="acct", expires_in=3600)

    async def fake_release(*_a: object, **_k: object) -> None:
        return None

    def fake_generate(**_k: object) -> GeneratedImage:
        return GeneratedImage(
            content=b"\x89PNG\r\n",
            content_type="image/png",
            source_url="https://example.invalid/x.png",
            metadata={},
        )

    monkeypatch.setattr(
        "app.services.generation.worker.build_session",
        lambda *_a, **_k: ProxySession(
            url="http://user:pass@127.0.0.1:9", sid="sid-test", masked="masked"
        ),
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.credentials_for_account", fake_creds
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", fake_release)
    monkeypatch.setattr("app.services.generation.worker.account_pool.bump_daily", AsyncMock())
    monkeypatch.setattr("app.services.generation.worker.generate_image", fake_generate)

    await process_job(client.app, job["id"])
    done = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "succeeded"
    assert done["result_urls"]
    logs = client.get("/api/admin/logs", headers=admin).json()["items"]
    logged = next(item for item in logs if item["job_id"] == job["id"])
    assert logged["upstream_urls"] == ["https://example.invalid/x.png"]
    assert client.get("/api/auth/me", headers=headers).json()["balance"] == 990
    rec = client.get(f"/api/admin/users/{user['user']['id']}/reconcile", headers=admin).json()
    assert rec["consistent"] is True


@pytest.mark.asyncio
async def test_worker_retries_proxy_tls_and_invalidates_arp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    await _mark_imported_hard(client)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "dana",
            "email": "dana@example.com",
            "password": "dana-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "dana", "password": "dana-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "gpt-image-2.5-flare",
            "prompt": "a red apple",
            "resolution": "1K",
            "idempotency_key": "worker-tls-01",
        },
    ).json()

    bundle = ArpBundle.model_validate(
        {
            "id": "arp_tls",
            "token": "dGVzdA==",
            "proxy_url": "http://127.0.0.1:9",
            "egress_id": "sid-tls",
            "ua": "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
            "ch": {},
            "headers": {},
            "minted_at": 1,
            "expires_at": 9_999_999_999,
            "uses": 1,
            "max_uses": 20,
        }
    )
    seen = {"generate": 0, "invalidated": []}

    class FakeArp:
        async def health(self) -> dict[str, object]:
            return {"ready": 8, "present": True}

        async def checkout(self) -> ArpBundle:
            return bundle

        async def invalidate(self, bundle_id: str) -> bool:
            seen["invalidated"].append(bundle_id)
            return True

    listed = client.get("/api/admin/accounts", headers=admin).json()["items"][0]
    account_id = int(listed["id"])

    async def fake_checkout(session: object, *_a: object, **_k: object) -> object:
        from app.models.account import UpstreamAccount

        row = await session.get(UpstreamAccount, account_id)  # type: ignore[union-attr]
        assert row is not None
        return row

    async def fake_creds(*_a: object, **_k: object) -> AdobeCredentials:
        return AdobeCredentials(access_token="tok", account_id="acct", expires_in=3600)

    async def fake_release(*_a: object, **_k: object) -> None:
        return None

    def fake_generate(**_k: object) -> GeneratedImage:
        seen["generate"] += 1
        if seen["generate"] < 2:
            raise AdobeGenerateError("proxy_tls", 502, "upstream connection dropped")
        return GeneratedImage(
            content=b"\x89PNG\r\n",
            content_type="image/png",
            source_url="https://example.invalid/x.png",
            metadata={},
        )

    monkeypatch.setattr("app.services.generation.worker._arp_store", lambda *_a, **_k: FakeArp())
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.credentials_for_account", fake_creds
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", fake_release)
    monkeypatch.setattr("app.services.generation.worker.account_pool.bump_daily", AsyncMock())
    monkeypatch.setattr("app.services.generation.worker.generate_image", fake_generate)

    await process_job(client.app, job["id"])
    done = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "succeeded"
    assert seen["generate"] == 2
    assert seen["invalidated"] == ["arp_tls"]


@pytest.mark.asyncio
async def test_alpha_408_reuses_one_account_and_token_without_parking(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={
            "text": "ims_sid=alpha-one; aux_sid=one\nims_sid=alpha-two; aux_sid=two",
            "group": "alpha",
        },
    )
    await _mark_imported_hard(client)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "alpha-user",
            "email": "alpha@example.com",
            "password": "alpha-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "alpha-user", "password": "alpha-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "gpt-image-2.5-flare",
            "prompt": "a blue square",
            "resolution": "1K",
            "idempotency_key": "alpha-408-budget",
        },
    ).json()

    bundles = [
        ArpBundle.model_validate(
            {
                "id": f"alpha_arp_{index}",
                "token": "dGVzdA==",
                "proxy_url": "http://user:pass@127.0.0.1:9",
                "egress_id": f"sid-alpha-{index}",
                "ua": "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
                "ch": {
                    "sec_ch_ua": '"Chromium";v="148"',
                    "sec_ch_ua_mobile": "?0",
                    "sec_ch_ua_platform": '"Windows"',
                },
                "headers": {},
                "minted_at": 1,
                "expires_at": 9_999_999_999,
                "uses": 0,
                "max_uses": 1,
            }
        )
        for index in range(3)
    ]
    seen: dict[str, object] = {"mint": 0, "submit": 0, "invalidated": []}
    alpha_account_id = client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"]

    class FakeArpStore:
        async def health(self) -> dict[str, int]:
            return {"ready": len(bundles)}

        async def checkout(self) -> ArpBundle | None:
            return bundles.pop(0) if bundles else None

        async def invalidate(self, item_id: str) -> bool:
            cast = seen["invalidated"]
            assert isinstance(cast, list)
            cast.append(item_id)
            return True

    async def fake_credentials(*_a: object, **_k: object) -> AdobeCredentials:
        seen["mint"] = int(seen["mint"]) + 1
        return AdobeCredentials("alpha-token", "alpha-account", 3600)

    def fake_generate(**_k: object) -> GeneratedImage:
        seen["submit"] = int(seen["submit"]) + 1
        raise AdobeGenerateError("arp_rejected", 408, "temporary refusal")

    async def fake_checkout(session: object, *_a: object, **_k: object) -> UpstreamAccount:
        row = await session.get(UpstreamAccount, alpha_account_id)  # type: ignore[union-attr]
        assert row is not None
        return row

    monkeypatch.setattr(
        "app.services.generation.worker._arp_store", lambda *_a, **_k: FakeArpStore()
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.credentials_for_account",
        fake_credentials,
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.bad_window_active",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_egress_outcome", AsyncMock()
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.clear_408_streak", AsyncMock()
    )
    monkeypatch.setattr("app.services.generation.worker.generate_image", fake_generate)

    await process_job(client.app, job["id"])
    assert seen["mint"] == 1
    assert seen["submit"] == 3
    assert seen["invalidated"] == ["alpha_arp_0", "alpha_arp_1", "alpha_arp_2"]
    listed = client.get("/api/admin/accounts", headers=admin).json()["items"]
    assert all(item["status"] == "active" for item in listed)


@pytest.mark.asyncio
async def test_normal_408_parks_and_switches_account_without_web_arp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={
            "text": "ims_sid=normal-one; aux_sid=one\nims_sid=normal-two; aux_sid=two",
            "group": "normal",
        },
    )
    await _mark_imported_hard(client)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "normal-user",
            "email": "normal@example.com",
            "password": "normal-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "normal-user", "password": "normal-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana-pro",
            "prompt": "a green square",
            "resolution": "1K",
            "idempotency_key": "normal-408-switch",
        },
    ).json()
    account_ids: list[int] = []
    bundles_seen: list[object] = []
    imported_ids = [
        item["id"] for item in client.get("/api/admin/accounts", headers=admin).json()["items"]
    ]

    async def fake_credentials(
        _session: object, row: UpstreamAccount, *_a: object, **_k: object
    ) -> AdobeCredentials:
        account_ids.append(row.id)
        return AdobeCredentials(
            "ios-token", f"normal-{row.id}", 3600, token_kind="ff_ios"
        )

    def fake_generate(**kwargs: object) -> GeneratedImage:
        bundles_seen.append(kwargs.get("bundle"))
        if len(bundles_seen) == 1:
            raise AdobeGenerateError("arp_rejected", 408, "temporary refusal")
        return GeneratedImage(
            content=b"\x89PNG\r\n\x1a\n",
            content_type="image/png",
            source_url="https://example.invalid/normal.png",
            metadata={},
        )

    async def fake_checkout(session: object, *_a: object, **_k: object) -> UpstreamAccount:
        row = await session.get(UpstreamAccount, imported_ids[len(account_ids)])  # type: ignore[union-attr]
        assert row is not None
        return row

    monkeypatch.setattr(
        "app.services.generation.worker.build_session",
        lambda *_a, **_k: ProxySession(
            url="http://user:pass@127.0.0.1:9", sid="normal-test", masked="masked"
        ),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.credentials_for_account",
        fake_credentials,
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.bad_window_active",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_408_hit",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_egress_outcome", AsyncMock()
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.clear_408_streak", AsyncMock()
    )
    monkeypatch.setattr("app.services.generation.worker.generate_image", fake_generate)

    await process_job(client.app, job["id"])
    assert len(account_ids) == 2
    assert account_ids[0] != account_ids[1]
    assert bundles_seen == [None, None]
    listed = client.get("/api/admin/accounts", headers=admin).json()["items"]
    assert {item["status"] for item in listed} == {"active", "parked"}


@pytest.mark.asyncio
async def test_429_does_not_park_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=rate-one; aux_sid=one\nims_sid=rate-two; aux_sid=two"},
    )
    await _mark_imported_hard(client)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "rate-user",
            "email": "rate@example.com",
            "password": "rate-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "rate-user", "password": "rate-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "gpt-image-2",
            "prompt": "a yellow square",
            "resolution": "1K",
            "idempotency_key": "rate-limit-no-park",
        },
    ).json()
    imported_ids = [
        item["id"] for item in client.get("/api/admin/accounts", headers=admin).json()["items"]
    ]
    generates = {"n": 0}

    async def fake_credentials(
        _session: object, row: UpstreamAccount, *_a: object, **_k: object
    ) -> AdobeCredentials:
        return AdobeCredentials("ios-token", f"mobile-{row.id}", 3600, token_kind="ff_ios")

    def fake_generate(**_kwargs: object) -> GeneratedImage:
        generates["n"] += 1
        raise AdobeGenerateError("rate_limited", 429, "limited")

    async def fake_checkout(session: object, *_a: object, **_k: object) -> UpstreamAccount:
        row = await session.get(UpstreamAccount, imported_ids[0])  # type: ignore[union-attr]
        assert row is not None
        return row

    monkeypatch.setattr(
        "app.services.generation.worker.build_session",
        lambda *_a, **_k: ProxySession(
            url="http://user:pass@127.0.0.1:9", sid="rate-test", masked="masked"
        ),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.credentials_for_account",
        fake_credentials,
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.bad_window_active",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_egress_outcome", AsyncMock()
    )
    monkeypatch.setattr("app.services.generation.worker.generate_image", fake_generate)
    monkeypatch.setattr("app.services.generation.worker.asyncio.sleep", AsyncMock())

    await process_job(client.app, job["id"])
    done = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "failed"
    assert done["error_code"] == "rate_limited"
    assert generates["n"] == 3
    listed = client.get("/api/admin/accounts", headers=admin).json()["items"]
    assert all(item["status"] == "active" for item in listed)


@pytest.mark.asyncio
async def test_451_retries_without_parking_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=safe-one; aux_sid=one\nims_sid=safe-two; aux_sid=two"},
    )
    await _mark_imported_hard(client)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "safe-user",
            "email": "safe@example.com",
            "password": "safe-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "safe-user", "password": "safe-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "gpt-image-2",
            "prompt": "a yellow square",
            "resolution": "1K",
            "idempotency_key": "content-reject-retry",
        },
    ).json()
    imported_ids = [
        item["id"] for item in client.get("/api/admin/accounts", headers=admin).json()["items"]
    ]
    generates = {"n": 0}

    async def fake_credentials(
        _session: object, row: UpstreamAccount, *_a: object, **_k: object
    ) -> AdobeCredentials:
        return AdobeCredentials("ios-token", f"mobile-{row.id}", 3600, token_kind="ff_ios")

    def fake_generate(**_kwargs: object) -> GeneratedImage:
        generates["n"] += 1
        raise AdobeGenerateError("content_rejected", 451, "unsafe")

    async def fake_checkout(session: object, *_a: object, **_k: object) -> UpstreamAccount:
        row = await session.get(UpstreamAccount, imported_ids[0])  # type: ignore[union-attr]
        assert row is not None
        return row

    monkeypatch.setattr(
        "app.services.generation.worker.build_session",
        lambda *_a, **_k: ProxySession(
            url="http://user:pass@127.0.0.1:9", sid="safe-test", masked="masked"
        ),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.credentials_for_account",
        fake_credentials,
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.bad_window_active",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_egress_outcome", AsyncMock()
    )
    monkeypatch.setattr("app.services.generation.worker.generate_image", fake_generate)
    monkeypatch.setattr("app.services.generation.worker.asyncio.sleep", AsyncMock())

    await process_job(client.app, job["id"])
    done = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "rejected"
    assert done["error_code"] == "content_rejected"
    assert generates["n"] == 3
    listed = client.get("/api/admin/accounts", headers=admin).json()["items"]
    assert all(item["status"] == "active" for item in listed)


@pytest.mark.asyncio
async def test_no_account_requeues_instead_of_failing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "queue-user",
            "email": "queue@example.com",
            "password": "queue-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "queue-user", "password": "queue-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana",
            "prompt": "a waiting square",
            "resolution": "1K",
            "idempotency_key": "queue-capacity-01",
        },
    ).json()
    delayed: list[str] = []

    async def fake_checkout(*_a: object, **_k: object) -> None:
        return None

    async def fake_later(_redis: object, job_id: str, _delay: float) -> None:
        delayed.append(job_id)

    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr("app.services.generation.worker.enqueue_later", fake_later)
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.bad_window_active",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_egress_outcome", AsyncMock()
    )

    await process_job(client.app, job["id"])
    done = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "queued"
    assert done["error_code"] is None
    assert delayed == [job["id"]]
    assert client.get("/api/auth/me", headers=headers).json()["balance"] == 992


@pytest.mark.asyncio
async def test_capacity_wait_is_not_a_request_attempt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "cap-user",
            "email": "cap@example.com",
            "password": "cap-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "cap-user", "password": "cap-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana",
            "prompt": "a waiting square",
            "resolution": "1K",
            "idempotency_key": "queue-capacity-not-attempt",
        },
    ).json()

    async def fake_checkout(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr("app.services.generation.worker.enqueue_later", AsyncMock())
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.bad_window_active",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.generation.worker.account_pool.record_egress_outcome", AsyncMock()
    )

    await process_job(client.app, job["id"])
    await process_job(client.app, job["id"])
    detail = client.get(f"/api/admin/logs/{job['id']}", headers=admin).json()
    assert detail["attempt_count"] == 0
    stored = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert stored["status"] == "queued"
    async with client.app.state.resources.request_sessions() as session:
        row = await session.get(GenerationJob, job["id"])
        assert row is not None
        assert row.retry_count == 0


@pytest.mark.asyncio
async def test_generation_timeout_fails_instead_of_looping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    _enable_dual_identity(client, admin)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "timeout-user",
            "email": "timeout@example.com",
            "password": "timeout-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "timeout-user", "password": "timeout-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    job = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana",
            "prompt": "a timed out square",
            "resolution": "1K",
            "idempotency_key": "queue-timeout-01",
        },
    ).json()
    delayed: list[str] = []

    async def fake_checkout(*_a: object, **_k: object) -> None:
        return None

    async def fake_later(_redis: object, job_id: str, _delay: float) -> None:
        delayed.append(job_id)

    monkeypatch.setattr(
        "app.services.generation.worker._generation_timed_out", lambda *_a, **_k: True
    )
    monkeypatch.setattr("app.services.generation.worker.account_pool.checkout", fake_checkout)
    monkeypatch.setattr("app.services.generation.worker.account_pool.release", AsyncMock())
    monkeypatch.setattr("app.services.generation.worker.enqueue_later", fake_later)

    await process_job(client.app, job["id"])
    done = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "failed"
    assert done["error_code"] == "poll_timeout"
    assert delayed == []
    assert client.get("/api/auth/me", headers=headers).json()["balance"] == 1000


def test_generation_timeout_clock_starts_at_create() -> None:
    from types import SimpleNamespace

    from app.services.generation.worker import _generation_age_seconds, _generation_timed_out

    created = datetime.now(UTC) - timedelta(seconds=120)
    job = SimpleNamespace(created_at=created, started_at=datetime.now(UTC))
    assert 100 <= _generation_age_seconds(job) <= 140
    assert _generation_timed_out(job, 30)
    assert not _generation_timed_out(job, 10_000)


@pytest.mark.asyncio
async def test_timeout_sweep_fails_orphaned_running_job(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/users",
        headers=admin,
        json={
            "username": "sweep-user",
            "email": "sweep@example.com",
            "password": "sweep-pass-1",
            "initial_balance": 1000,
        },
    )
    token = client.post(
        "/api/auth/login", json={"username": "sweep-user", "password": "sweep-pass-1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    stale = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana",
            "prompt": "orphaned running job",
            "resolution": "1K",
            "idempotency_key": "sweep-timeout-01",
        },
    ).json()
    fresh = client.post(
        "/api/gen/image",
        headers=headers,
        json={
            "model": "nano-banana",
            "prompt": "still within budget",
            "resolution": "1K",
            "idempotency_key": "sweep-timeout-02",
        },
    ).json()

    async with client.app.state.resources.request_sessions() as session:
        row = await session.get(GenerationJob, stale["id"])
        assert row is not None
        row.status = "running"
        row.created_at = datetime.now(UTC) - timedelta(seconds=400)
        other = await session.get(GenerationJob, fresh["id"])
        assert other is not None
        other.status = "running"
        await session.commit()

    before = client.get("/api/auth/me", headers=headers).json()["balance"]
    assert await sweep_timed_out_jobs(client.app) == 1
    done = client.get(f"/api/jobs/{stale['id']}", headers=headers).json()
    assert done["status"] == "failed"
    assert done["error_code"] == "poll_timeout"
    alive = client.get(f"/api/jobs/{fresh['id']}", headers=headers).json()
    assert alive["status"] == "running"
    assert client.get("/api/auth/me", headers=headers).json()["balance"] > before


@pytest.mark.asyncio
async def test_credentials_tls_does_not_mark_dead(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])

    def fake_exchange(**_k: object) -> AdobeCredentials:
        raise AdobeAuthError("upstream connection dropped", code="proxy_tls")

    monkeypatch.setattr("app.services.accounts.exchange_cookie_for_clio", fake_exchange)
    from app.models.account import UpstreamAccount
    from app.services import accounts as account_pool

    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        row = await session.get(UpstreamAccount, account_id)
        assert row is not None
        with pytest.raises(AdobeGenerateError) as error:
            await account_pool.credentials_from_cookie(
                session,
                row,
                resources.cipher,
                proxy_url="http://127.0.0.1:9",
                user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
                impersonate="chrome146",
            )
        assert error.value.code == "proxy_tls"
        stored = await session.get(UpstreamAccount, account_id)
        assert stored is not None
        assert stored.status == "active"
        assert stored.credential_status == "transient"


@pytest.mark.asyncio
async def test_ims_429_does_not_mark_account_dead(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=rate-limited; other=abc", "group": "alpha"},
    )
    account_id = client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"]

    def fake_exchange(**_k: object) -> AdobeCredentials:
        raise AdobeAuthError("rate limited", code="rate_limited", status_code=429)

    monkeypatch.setattr("app.services.accounts.exchange_cookie_for_clio", fake_exchange)
    from app.services import accounts as account_pool

    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        row = await session.get(UpstreamAccount, account_id)
        assert row is not None
        with pytest.raises(AdobeGenerateError) as error:
            await account_pool.credentials_from_cookie(
                session,
                row,
                resources.cipher,
                proxy_url="http://127.0.0.1:9",
                user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
                impersonate="chrome146",
            )
        assert error.value.code == "rate_limited"
        assert row.status == "active"
        assert row.credential_status == "transient"


@pytest.mark.asyncio
async def test_explicit_invalid_credential_is_the_only_auth_dead_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=invalid-credential; other=abc", "group": "alpha"},
    )
    account_id = client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"]

    def fake_exchange(**_k: object) -> AdobeCredentials:
        raise AdobeAuthError("invalid credentials", code="auth_invalid", status_code=401)

    monkeypatch.setattr("app.services.accounts.exchange_cookie_for_clio", fake_exchange)
    from app.services import accounts as account_pool

    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        row = await session.get(UpstreamAccount, account_id)
        assert row is not None
        with pytest.raises(AdobeGenerateError):
            await account_pool.credentials_from_cookie(
                session,
                row,
                resources.cipher,
                proxy_url="http://127.0.0.1:9",
                user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
                impersonate="chrome146",
            )
        assert row.status == "dead"
        assert row.credential_status == "invalid"


@pytest.mark.asyncio
async def test_ff_ios_persists_only_encrypted_device_grant(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=normal-device; aux_sid=aux", "group": "normal"},
    )
    account_id = client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"]

    def fake_mint(**_kwargs: object) -> FFIOSCredentials:
        return FFIOSCredentials(
            access_token="short-access-token",
            account_id="normal-account",
            expires_in=3600,
            device_token="long-device-token",
            device_id="stable-device-id",
        )

    monkeypatch.setattr("app.services.accounts.mint_ff_ios", fake_mint)
    from app.services import accounts as account_pool

    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        row = await session.get(UpstreamAccount, account_id)
        assert row is not None
        creds = await account_pool.credentials_for_account(
            session,
            row,
            resources.cipher,
            proxy_url="http://user:pass@127.0.0.1:9",
            user_agent="unused-for-ios",
            impersonate="chrome146",
            identity="mobile",
        )
        assert creds.access_token == "short-access-token"
        assert row.access_token_enc is None
        assert row.ios_device_token_enc != "long-device-token"
        assert resources.cipher.decrypt(row.ios_device_token_enc or "") == "long-device-token"
        assert resources.cipher.decrypt(row.ios_device_id_enc or "") == "stable-device-id"


@pytest.mark.asyncio
async def test_list_unparks_expired_but_keeps_open_ended_park(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123\nims_sid=othervalue456; other=xyz789"},
    )
    from sqlalchemy import select

    from app.models.account import UpstreamAccount

    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        rows = list(await session.scalars(select(UpstreamAccount).order_by(UpstreamAccount.id)))
        rows[0].status = "parked"
        rows[0].parked_until = datetime.now(UTC) - timedelta(seconds=30)
        rows[1].status = "parked"
        rows[1].parked_until = None
        await session.commit()
        expired_id, sticky_id = int(rows[0].id), int(rows[1].id)
    listed = {
        item["id"]: item
        for item in client.get("/api/admin/accounts", headers=admin).json()["items"]
    }
    assert listed[expired_id]["status"] == "active"
    assert listed[expired_id]["parked_until"] is None
    assert listed[sticky_id]["status"] == "parked"
    assert listed[sticky_id]["parked_until"] is None


class _MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def get(self, key: str) -> object | None:
        return self.data.get(key)

    async def set(self, key: str, value: object, ex: int | None = None) -> bool:
        self.data[key] = value
        return True

    async def incr(self, key: str) -> int:
        self.data[key] = int(self.data.get(key) or 0) + 1
        return int(self.data[key])

    async def expire(self, key: str, _seconds: int) -> bool:
        return key in self.data

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


def test_identity_lane_follows_model_not_account_group() -> None:
    from app.services.generation.worker import identity_lane

    assert identity_lane("gpt-image-2.5-flare", dual_identity=True) == "web"
    assert identity_lane("gpt-image-2.5-sunburst", dual_identity=True) == "web"
    assert identity_lane("nano-banana", dual_identity=True) == "mobile"
    assert identity_lane("nano-banana-pro", dual_identity=True) == "mobile"
    assert identity_lane("gpt-image-2", dual_identity=True) == "mobile"
    assert identity_lane("nano-banana", dual_identity=False) == "web"
    assert identity_lane("gpt-image-2.5-flare", dual_identity=False) == "web"


@pytest.mark.asyncio
async def test_checkout_does_not_filter_by_account_group(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.services import accounts as account_pool
    from app.services.generation.worker import _checkout_for_job

    seen: list[str | None] = []

    async def fake_checkout(_session: object, _redis: object, **kwargs: object) -> object:
        seen.append(kwargs.get("group"))  # type: ignore[arg-type]
        return SimpleNamespace(id=15, group="alpha")

    monkeypatch.setattr(account_pool, "checkout", fake_checkout)
    cfg = SimpleNamespace(
        account_daily_image_limit=1000,
        account_concurrency_advanced=1,
        account_concurrency_normal=4,
        blocked_408_models=lambda: set(),
        account_408_block_after=1,
        alpha_canary_account_id=0,
        normal_canary_account_id=0,
    )
    row = await _checkout_for_job(
        None,
        None,
        cfg,
        model="nano-banana",
        resolution="1K",
        dual_identity=True,
        sticky_account_id=None,
        in_window=False,
    )
    assert seen == [None]
    assert row is not None and row.id == 15

    seen.clear()
    await _checkout_for_job(
        None,
        None,
        cfg,
        model="gpt-image-2.5-flare",
        resolution="1K",
        dual_identity=True,
        sticky_account_id=None,
        in_window=False,
    )
    assert seen == [None]


@pytest.mark.asyncio
async def test_one_normal_and_one_alpha_canary_stay_isolated(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=canary-normal; aux_sid=n", "group": "normal"},
    )
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=canary-alpha; aux_sid=a", "group": "alpha"},
    )
    await _mark_imported_hard(client)
    listed = client.get("/api/admin/accounts", headers=admin).json()["items"]
    ids = {item["group"]: item["id"] for item in listed}
    from app.services import accounts as account_pool

    resources = client.app.state.resources
    redis = _MemRedis()
    async with resources.request_sessions() as session:
        normal = await account_pool.checkout(
            session,
            redis,  # type: ignore[arg-type]
            daily_limit=5,
            group="normal",
            account_id=ids["normal"],
        )
        alpha = await account_pool.checkout(
            session,
            redis,  # type: ignore[arg-type]
            daily_limit=5,
            group="alpha",
            account_id=ids["alpha"],
        )
    assert normal is not None and normal.id == ids["normal"] and normal.group == "normal"
    assert alpha is not None and alpha.id == ids["alpha"] and alpha.group == "alpha"


@pytest.mark.asyncio
async def test_408_window_trips_and_observe_helpers() -> None:
    from app.services import accounts as pool

    redis = _MemRedis()
    hit = dict(window_seconds=60, threshold=3, pause_seconds=120)
    assert await pool.bad_window_active(redis) is False
    assert await pool.record_408_hit(redis, **hit) is False
    assert await pool.record_408_hit(redis, **hit) is False
    assert await pool.record_408_hit(redis, **hit) is True
    assert await pool.bad_window_active(redis) is True
    await pool.mark_observing(redis, 9, 30)
    assert await pool.is_observing(redis, 9) is True
    await pool.clear_408_streak(redis, 9)
    assert await pool.is_observing(redis, 9) is False


def test_park_seconds_escalate_and_never_dead() -> None:
    from app.services.accounts import park_seconds_for_streak

    assert park_seconds_for_streak(1, first=300, second=600, rest=1800, disable_after=3) == 300
    assert park_seconds_for_streak(2, first=300, second=600, rest=1800, disable_after=3) == 600
    assert park_seconds_for_streak(3, first=300, second=600, rest=1800, disable_after=3) == 1800
    assert park_seconds_for_streak(8, first=300, second=600, rest=1800, disable_after=3) == 1800


def test_import_email_prefix_is_masked(client: TestClient) -> None:
    admin = _admin(client)
    res = client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ada@example.com----ims_sid=secretvalue123; other=abc123"},
    )
    assert res.status_code == 200, res.text
    item = client.get("/api/admin/accounts", headers=admin).json()["items"][0]
    assert item["email_masked"] == "ad***@example.com"
    assert item["cookie"] == "ims_sid=secretvalue123; other=abc123"
    assert "ada@example.com" not in item["email_masked"]
    exported = client.get("/api/admin/accounts/export", headers=admin).json()
    assert exported["count"] == 1
    assert exported["text"].startswith("ad***@example.com----")
    assert "ims_sid=secretvalue123" in exported["text"]
    assert "ada@example.com" not in exported["text"]
    again = client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    assert again.json()["created"] == 0
    assert again.json()["skipped"] == 1


def test_delete_selected_accounts_and_protect_canary(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={
            "text": "ims_sid=delete-one; other=a\nims_sid=delete-two; other=b",
            "group": "normal",
        },
    )
    items = client.get("/api/admin/accounts", headers=admin).json()["items"]
    first_id, second_id = items[0]["id"], items[1]["id"]
    cfg = client.get("/api/admin/config", headers=admin).json()
    cfg["normal_canary_account_id"] = first_id
    assert client.put("/api/admin/config", headers=admin, json=cfg).status_code == 200

    protected = client.request(
        "DELETE",
        "/api/admin/accounts",
        headers=admin,
        json={"ids": [first_id, second_id]},
    )
    assert protected.status_code == 409
    assert client.get("/api/admin/accounts", headers=admin).json()["total"] == 2

    cfg["normal_canary_account_id"] = 0
    assert client.put("/api/admin/config", headers=admin, json=cfg).status_code == 200
    deleted = client.request(
        "DELETE",
        "/api/admin/accounts",
        headers=admin,
        json={"ids": [second_id]},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": 1}
    remaining = client.get("/api/admin/accounts", headers=admin).json()["items"]
    assert [item["id"] for item in remaining] == [first_id]


@pytest.mark.asyncio
async def test_credentials_from_cookie_are_not_stored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])

    def fake_exchange(**_k: object) -> AdobeCredentials:
        return AdobeCredentials(
            access_token="live-token-not-for-db", account_id="acct", expires_in=3600
        )

    monkeypatch.setattr("app.services.accounts.exchange_cookie_for_clio", fake_exchange)
    from app.models.account import UpstreamAccount
    from app.services import accounts as account_pool

    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        row = await session.get(UpstreamAccount, account_id)
        assert row is not None
        creds = await account_pool.credentials_from_cookie(
            session,
            row,
            resources.cipher,
            proxy_url="http://127.0.0.1:9",
            user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
            impersonate="chrome146",
        )
        await session.commit()
        assert creds.access_token == "live-token-not-for-db"
        stored = await session.get(UpstreamAccount, account_id)
        assert stored is not None
        assert stored.access_token_enc is None
        assert stored.token_expires_at is None


def test_list_accounts_filters_by_status_and_credits(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "user@example.com----ims_sid=secretvalue123; other=abc123"},
    )
    item = client.get("/api/admin/accounts", headers=admin).json()["items"][0]
    account_id = int(item["id"])
    client.patch(f"/api/admin/accounts/{account_id}", headers=admin, json={"status": "dead"})

    listed = client.get("/api/admin/accounts", headers=admin, params={"status": "dead"}).json()
    assert listed["total"] == 1
    assert listed["credits_available"] == 0
    assert listed["accounts_total"] == 1
    assert listed["accounts_parked"] == 0
    assert listed["accounts_hard"] == 0
    empty = client.get("/api/admin/accounts", headers=admin, params={"status": "active"}).json()
    assert empty["total"] == 0
    assert empty["credits_available"] == 0
    assert empty["accounts_total"] == 1
    by_email = client.get("/api/admin/accounts", headers=admin, params={"q": "example.com"}).json()
    assert by_email["total"] == 1
    low = client.get("/api/admin/accounts", headers=admin, params={"credits_lt": 1}).json()
    assert low["total"] == 1
    high = client.get("/api/admin/accounts", headers=admin, params={"credits_lt": 0}).json()
    assert high["total"] == 0


def test_patch_account_replaces_cookie_and_echoes_plain(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])
    res = client.patch(
        f"/api/admin/accounts/{account_id}",
        headers=admin,
        json={
            "status": "parked",
            "cookie": "fresh@example.com----ims_sid=brandnewcookie; other=xyz",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "parked"
    assert body["cookie"] == "ims_sid=brandnewcookie; other=xyz"
    assert "secretvalue123" not in body["cookie"]
    assert body["email_masked"].endswith("@example.com")


def test_refresh_account_stores_credit_snapshot_not_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123", "group": "alpha"},
    )
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])

    def fake_exchange(**_k: object) -> AdobeCredentials:
        return AdobeCredentials(
            access_token="live-token-not-for-db", account_id="acct", expires_in=3600
        )

    from app.adobe.credits import CreditBalance

    def fake_credits(**_k: object) -> CreditBalance:
        return CreditBalance(remaining=42, used=8, total=50, plan_cap="HARD")

    monkeypatch.setattr("app.api.admin_accounts.lease_panel_proxy", AsyncMock(return_value="http://127.0.0.1:9"))
    monkeypatch.setattr("app.services.accounts.exchange_cookie_for_clio", fake_exchange)
    monkeypatch.setattr("app.services.accounts.fetch_credits_balance", fake_credits)

    res = client.post(f"/api/admin/accounts/{account_id}/refresh", headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credits_remaining"] == 42
    assert body["credits_total"] == 50
    assert body["plan_cap"] == "HARD"
    assert "live-token-not-for-db" not in str(body)


def test_refresh_account_retries_proxy_tls_three_times(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123", "group": "alpha"},
    )
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])
    calls = {"lease": 0, "refresh": 0}

    async def fake_lease(_store: object) -> str:
        calls["lease"] += 1
        return f"http://127.0.0.1:{calls['lease']}"

    async def fake_refresh(
        session: object, row: UpstreamAccount, *_a: object, **_k: object
    ) -> UpstreamAccount:
        calls["refresh"] += 1
        if calls["refresh"] < 3:
            raise AdobeGenerateError("proxy_tls", 502, "dropped")
        row.credits_remaining = 11000
        row.credits_total = 11000
        row.plan_cap = "HARD"
        row.credential_status = "ready"
        return row

    monkeypatch.setattr("app.api.admin_accounts.lease_panel_proxy", fake_lease)
    monkeypatch.setattr("app.api.admin_accounts.account_pool.refresh_credits", fake_refresh)
    monkeypatch.setattr("app.api.admin_accounts.asyncio.sleep", AsyncMock())

    res = client.post(f"/api/admin/accounts/{account_id}/refresh", headers=admin)
    assert res.status_code == 200, res.text
    assert calls["lease"] == 3
    assert calls["refresh"] == 3
    assert res.json()["credits_remaining"] == 11000


def test_refresh_account_does_not_retry_rate_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123", "group": "alpha"},
    )
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])
    calls = {"refresh": 0}

    async def fake_refresh(*_a: object, **_k: object) -> UpstreamAccount:
        calls["refresh"] += 1
        raise AdobeGenerateError("rate_limited", 429, "limited")

    monkeypatch.setattr(
        "app.api.admin_accounts.lease_panel_proxy", AsyncMock(return_value="http://127.0.0.1:9")
    )
    monkeypatch.setattr("app.api.admin_accounts.account_pool.refresh_credits", fake_refresh)

    res = client.post(f"/api/admin/accounts/{account_id}/refresh", headers=admin)
    assert res.status_code == 429
    assert calls["refresh"] == 1


@pytest.mark.asyncio
async def test_checkout_skips_hot_model_while_streaked(client: TestClient) -> None:
    admin = _admin(client)
    client.post(
        "/api/admin/accounts/import",
        headers=admin,
        json={"text": "ims_sid=secretvalue123; other=abc123"},
    )
    await _mark_imported_hard(client)
    account_id = int(client.get("/api/admin/accounts", headers=admin).json()["items"][0]["id"])
    from app.services import accounts as account_pool

    redis = _MemRedis()
    redis.data[f"{account_pool.STREAK_PREFIX}{account_id}"] = 2
    resources = client.app.state.resources
    async with resources.request_sessions() as session:
        blocked = await account_pool.checkout(
            session,
            redis,  # type: ignore[arg-type]
            daily_limit=100,
            model="gpt-image-2.5-flare",
            block_models={"gpt-image-2.5-flare"},
            block_after=1,
        )
        ok = await account_pool.checkout(
            session,
            redis,  # type: ignore[arg-type]
            daily_limit=100,
            model="nano-banana",
            block_models={"gpt-image-2.5-flare"},
            block_after=1,
        )
        await session.commit()
    assert blocked is None
    assert ok is not None and ok.id == account_id

