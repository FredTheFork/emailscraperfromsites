from __future__ import annotations

import asyncio
import threading
import time

import pytest
from aiohttp import web
from fastapi.testclient import TestClient
from test_http import server

from publicmail.http import Fetcher
from publicmail.models import FetchError
from publicmail.store import Store
from publicmail.urls import site_key
from publicmail.web import BodyLimitMiddleware, create_app

TOKEN = "runtime-test-access-key-not-a-production-key"


@pytest.mark.parametrize("http_status", [429, 503])
async def test_shared_http_cooldown_survives_client_restart(store, fast_settings, http_status):
    seen = []

    async def handler(request):
        seen.append(request.path)
        return web.Response(status=http_status, headers={"Retry-After": "120"})

    async with server(handler) as base:
        async with Fetcher(fast_settings, allow_private=True) as first:
            first.cooldown_store = store
            result = await first.fetch(base + "/limited")
            assert result.status == http_status
            delay, status = store.get_cooldown(site_key(base))
            assert delay > 119
            assert status == http_status
        async with Fetcher(fast_settings, allow_private=True) as restarted:
            restarted.cooldown_store = store
            with pytest.raises(FetchError) as caught:
                await restarted.fetch(base + "/other-path")
            assert caught.value.status == http_status
            assert caught.value.retry_after > 119
        assert seen == ["/limited"]


def test_persisted_cooldown_is_not_shortened(store):
    until = time.time() + 1000
    store.put_cooldown("acmeglazing.co.uk", until, 429)
    store.put_cooldown("acmeglazing.co.uk", time.time() + 10, 503)
    delay, status = store.get_cooldown("acmeglazing.co.uk")
    assert delay > 999
    assert status == 429


def test_unexpected_worker_exit_notifies_supervisor_and_fails_health(tmp_path):
    app = create_app(tmp_path / "supervised.db", token=TOKEN)
    notified = threading.Event()
    app.state.on_worker_failure = notified.set
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        client.portal.call(lambda: app.state.worker_task.cancel())
        assert notified.wait(3)
        assert app.state.worker_failed
        assert client.get("/healthz").status_code == 503


def test_readonly_startup_cannot_resume_a_previously_running_queue(tmp_path, monkeypatch):
    path = tmp_path / "readonly.db"
    store = Store(path)
    store.add_sites([{"domain": "acmeglazing.co.uk", "input_url": "https://acmeglazing.co.uk/"}])
    store.control("start")
    store.close()
    monkeypatch.setenv("SCRAPER_READ_ONLY", "1")
    app = create_app(path, token=TOKEN)
    with TestClient(app) as client:
        status = client.get("/api/status", headers={"Authorization": "Bearer " + TOKEN}).json()
        assert status["mode"] == "paused"
        assert status["statuses"]["pending"] == 1
        assert status["statuses"]["running"] == 0
        assert status["pages_scanned"] == 0


async def test_streamed_body_limit_is_enforced_without_content_length():
    sent = []

    async def downstream(scope, receive, send):
        while message := await receive():
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"a" * 40_000, "more_body": True}

    async def send(message):
        sent.append(message)

    await BodyLimitMiddleware(downstream)({"type": "http", "path": "/api/login"}, receive, send)
    assert sent[0]["status"] == 413


async def test_cooldown_does_not_make_an_unrelated_website_wait(fast_settings):
    async with Fetcher(fast_settings) as client:
        client.limiter.cooldown("https://acmeglazing.co.uk/", 600, 429)
        with pytest.raises(FetchError):
            client.limiter.check_cooldown("https://shop.acmeglazing.co.uk/")
        await asyncio.wait_for(client.limiter.wait("https://unrelatedglazing.com/"), timeout=0.5)
