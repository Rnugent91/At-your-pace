import os
import shutil
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app

CHROMIUM = os.getenv("CHROMIUM_PATH") or (
    "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else shutil.which("chromium")
)


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), data_dir=tmp_path, demo_mode=True, app_password="",
                   anthropic_api_key="", chromium_path=CHROMIUM or "")


def new_quote(client, **extra):
    data = {"client_name": "Jane Smith", "adults": "2", "children": "1", "cruise_line": "Royal Caribbean",
            "ship": "Utopia of the Seas", "sail_date": "2026-10-26", "tracking_enabled": "on", "tracking_fee": "50"}
    resp = client.post("/quotes", data={**data, **extra}, follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"].rsplit("/", 1)[1]


def test_password_protects_everything_but_health(settings):
    client = TestClient(create_app(replace(settings, app_password="s3cret"), direct_providers=[]))
    assert client.get("/healthz").status_code == 200
    assert client.get("/quotes").status_code == 401
    assert client.get("/quotes", auth=("advisor", "wrong")).status_code == 401
    assert client.get("/quotes", auth=("advisor", "s3cret")).status_code == 200


def test_quote_flow(settings):
    client = TestClient(create_app(settings, direct_providers=[]))
    qid = new_quote(client)
    page = client.get(f"/quotes/{qid}").text
    assert "Ocean View Balcony" in page and "Deluxe Beverage Package" in page
    # cheapest room in each category is pre-selected
    assert page.count('name="room"') == 4 and page.count("checked") >= 4

    resp = client.post(f"/quotes/{qid}", data={
        "client_name": "Jane Smith", "adults": "2", "children": "1", "tracking_enabled": "on", "tracking_fee": "50",
        "room": ["Balcony|Ocean View Balcony"], "room_price_2": "650", "addon_0": "on", "qty_0": "",
        "client_intro": "Hello Jane", "valid_until": "2026-10-02",
    }, follow_redirects=False)
    assert resp.status_code == 303

    preview = client.get(f"/quotes/{qid}/preview").text
    assert "Hello Jane" in preview and "SAMPLE QUOTE" in preview
    assert "$650.00" in preview  # advisor's price override
    # 650 × 3 + 95 × 3 + 89.99 × 9 + 50
    assert "$3,094.91" in preview
    assert "Junior Suite" not in preview  # unselected room hidden

    listing = client.get("/quotes").text
    assert "Jane Smith" in listing


def test_recheck_records_price_history(settings):
    client = TestClient(create_app(settings, direct_providers=[]))
    qid = new_quote(client)
    client.post(f"/quotes/{qid}/recheck")
    assert "ready" in client.get(f"/quotes/{qid}").text


def test_bad_date_rejected(settings):
    client = TestClient(create_app(settings, direct_providers=[]))
    assert client.post("/quotes", data={"client_name": "x", "ship": "y", "sail_date": "soon"}).status_code == 400


@pytest.mark.skipif(not CHROMIUM, reason="Chromium not available")
def test_pdf_download(settings):
    client = TestClient(create_app(settings, direct_providers=[]))
    qid = new_quote(client)
    resp = client.get(f"/quotes/{qid}/pdf")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
    assert "Jane Smith" in resp.headers["content-disposition"]


def test_cross_site_posts_blocked(settings):
    client = TestClient(create_app(settings, direct_providers=[]))
    qid = new_quote(client)
    evil = client.post(f"/quotes/{qid}/delete", headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert evil.status_code == 403
    ok = client.post(f"/quotes/{qid}/delete", headers={"origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303
