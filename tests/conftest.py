from __future__ import annotations

import inspect

import pytest

from publicmail.config import CrawlSettings
from publicmail.http import RateLimiter
from publicmail.importer import prepare_row
from publicmail.models import FetchError, FetchResult
from publicmail.store import Store

HOME = "https://acmeglazing.co.uk/"


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "state.db")
    yield database
    database.close()


@pytest.fixture
def seeded(store):
    store.add_sites([prepare_row({"domain": "acmeglazing.co.uk", "business_name": "Acme Glazing"})])
    return store.get_site(1)


@pytest.fixture
def fast_settings():
    # Test-only zero pacing keeps the suite fast. Public settings enforce >=0.5s.
    return CrawlSettings().model_copy(
        update={"delay": 0.0, "max_pages": 20, "max_sitemaps": 3, "retry_base": 1.0}
    )


def response(url, body="", status=200, content_type="text/html", headers=None):
    return FetchResult(
        url,
        status,
        {"content-type": content_type, **(headers or {})},
        body.encode() if isinstance(body, str) else body,
        content_type,
    )


class FakeFetcher:
    def __init__(self, settings, responses=None):
        self.settings = settings
        self.responses = responses or {}
        self.calls = []
        self.allow_private = False
        self.limiter = RateLimiter(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, url, *, guard=None, redirect_check=None, max_bytes=None):
        if guard:
            await guard(url)
        self.calls.append(url)
        result = self.responses.get(url, response(url, status=404))
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if isinstance(result, FetchError):
            raise result
        if callable(result):
            result = result(url)
        if inspect.isawaitable(result):
            result = await result
        return result
