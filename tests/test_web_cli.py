from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from publicmail.web import create_app

TOKEN = "test-access-key-at-least-twenty-characters"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "web.db", token=TOKEN, run_worker=False)) as session:
        yield session


def headers():
    return {"Authorization": "Bearer " + TOKEN}


def test_health_static_and_authenticated_data(client):
    assert client.get("/healthz").json()["ok"]
    index = client.get("/")
    assert index.status_code == 200
    assert "Public Mail" in index.text
    assert "default-src 'self'" in index.headers["content-security-policy"]
    assert "x-frame-options" not in index.headers
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=headers()).json()["total"] == 0
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    # Login does NOT set a token-bearing cookie or expose the key in a URL.
    assert not client.cookies


def test_login_rate_limit(client):
    for _ in range(5):
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"token": "wrong"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_upload_import_export_and_search(client):
    data = b'domain,url,business_name\nacmeglazing.co.uk,https://acmeglazing.co.uk,"=1+1"\n'
    response = client.post(
        "/api/import", headers=headers(), files={"file": ("domains.csv", data, "text/csv")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] == 1
    assert (
        client.post("/api/import", headers=headers(), files={"file": ("domains.csv", data)}).json()[
            "duplicates"
        ]
        == 1
    )
    sites = client.get("/api/sites?q=acme", headers=headers()).json()
    assert sites["total"] == 1
    assert sites["items"][0]["business_name"] == "=1+1"
    assert client.get("/api/sites/1/pages", headers=headers()).json() == []
    export = client.get("/api/export/coverage", headers=headers())
    assert export.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
    assert rows[0]["business_name"] == "'=1+1"
    assert client.get("/api/export/not-real", headers=headers()).status_code == 404
    assert client.get("/api/sites?size=1000", headers=headers()).status_code == 422
    assert client.get("/api/sites?status=garbage", headers=headers()).status_code == 400


def test_mutations_origin_and_settings_protections(client):
    assert (
        client.post(
            "/api/control", headers={**headers(), "Origin": "https://evil.org"}, json={"action": "start"}
        ).status_code
        == 403
    )
    assert client.post("/api/control", headers=headers(), json={"action": "delete"}).status_code == 400
    settings = client.get("/api/settings", headers=headers()).json()
    assert (
        client.put("/api/settings", headers=headers(), json={**settings, "allow_private": True}).status_code
        == 422
    )
    assert client.put("/api/settings", headers=headers(), json={**settings, "delay": 0}).status_code == 422
    assert (
        client.post("/api/control", headers=headers(), json={"action": "start", "limit": 100}).status_code
        == 200
    )
    assert client.put("/api/settings", headers=headers(), json=settings).status_code == 409
    assert client.post("/api/retry", headers=headers(), json={"statuses": ["no_email"]}).status_code == 409
    assert client.post("/api/control", headers=headers(), json={"action": "pause"}).status_code == 200
    assert (
        client.put("/api/settings", headers=headers(), json={**settings, "max_pages": 50}).status_code == 200
    )


def test_large_uploads_and_unauthenticated_body_are_rejected_early(client):
    assert client.post("/api/import", files={"file": ("domains.csv", b"bad")}).status_code == 401
    response = client.post(
        "/api/import", headers={**headers(), "Content-Length": str(300 * 1024 * 1024)}, content=b"x"
    )
    assert response.status_code == 413
    assert (
        client.post("/api/import", headers=headers(), files={"file": ("bad.txt", b"bad")}).status_code == 400
    )
    assert (
        client.post(
            "/api/import", headers=headers(), files={"file": ("bad.csv", b"unexpected,columns\nx,y\n")}
        ).status_code
        == 400
    )


def test_readonly_preview_does_not_allow_start(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_READ_ONLY", "1")
    with TestClient(create_app(tmp_path / "readonly.db", token=TOKEN, run_worker=False)) as client:
        assert client.get("/api/status", headers=headers()).json()["read_only"]
        assert client.post("/api/control", headers=headers(), json={"action": "start"}).status_code == 403
        assert client.get("/api/export/coverage", headers=headers()).status_code == 200


def test_cli_import_status_export_and_errors(tmp_path):
    input_file, db = tmp_path / "input.csv", tmp_path / "state.db"
    input_file.write_text("domain\nacmeglazing.co.uk\n", encoding="utf-8")

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "publicmail.cli", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "SCRAPER_DB": str(db)},
            timeout=20,
        )

    assert run("--help").returncode == 0
    result = run("import", str(input_file))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["inserted"] == 1
    assert json.loads(run("status", "--json").stdout)["total"] == 1
    assert run("export", "--out", str(tmp_path / "exports"), "--jsonl").returncode == 0
    assert (tmp_path / "exports" / "coverage.csv").is_file()
    assert run("run", "--max-sites", "-1").returncode == 1
    assert run("import", str(tmp_path / "missing.csv")).returncode == 1
