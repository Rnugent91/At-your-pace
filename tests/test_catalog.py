from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog import CatalogStore
from app.config import load_settings
from app.main import create_app
from app.models import SailingResearch, Stateroom
from app.providers.base import CatalogSailing
from app.sync import refresh_rooms, refresh_watched, sync_catalog

SOON = (date.today() + timedelta(days=60)).isoformat()
LATER = (date.today() + timedelta(days=120)).isoformat()


def sailing(key, sail_date=SOON, ship="Utopia of the Seas", **prices):
    return CatalogSailing(
        cruise_line="Royal Caribbean", sailing_key=key, ship=ship, ship_code="UT", sail_date=sail_date, nights=4,
        itinerary_name="Bahamas & Perfect Day", departure_port="Port Canaveral", ports=["Perfect Day CocoCay", "Nassau"],
        prices=prices or {"Interior": 500.0, "Balcony": 700.0, "Suite": None}, taxes_fees_per_person=105.0,
    )


def room(name, category, price, code=None):
    return Stateroom(category=category, name=name, code=code, price_per_person=price, taxes_fees_per_person=None,
                     sold_out=price is None, notes=None, source="test")


class FakeLine:
    name = "Fake RC"
    cruise_line = "Royal Caribbean"

    def __init__(self, rows, rooms=None):
        self.rows, self.rooms, self.research_calls = rows, rooms or [], []

    def handles(self, line):
        return "royal" in line.lower()

    def iter_catalog(self):
        yield from self.rows

    def research(self, req):
        self.research_calls.append(req)
        return SailingResearch(cruise_line="Royal Caribbean", ship=req.ship, sail_date=req.sail_date, nights=4,
                               departure_port=None, itinerary_name=None, itinerary=[], currency="USD",
                               staterooms=self.rooms, addons=[], sources=[], warnings=[])


class BrokenLine(FakeLine):
    cruise_line = "Carnival"

    def iter_catalog(self):
        raise RuntimeError("site down")
        yield


def test_upsert_tracks_changes_and_removals(tmp_path):
    store = CatalogStore(tmp_path)
    r = store.upsert("Royal Caribbean", [sailing("A"), sailing("B", LATER)], today="2026-01-01")
    assert (r.sailings, r.changed, r.removed) == (2, 0, 0)

    # Same prices again: nothing recorded as a change.
    r = store.upsert("Royal Caribbean", [sailing("A"), sailing("B", LATER)], today="2026-01-02")
    assert r.changed == 0
    assert len(store.class_history("Royal Caribbean", "A")) == 3

    # Interior drops on A; B disappears (sold out / removed by the line).
    r = store.upsert("Royal Caribbean", [sailing("A", Interior=450.0, Balcony=700.0, Suite=None)], today="2026-01-03")
    assert (r.changed, r.removed) == (1, 1)
    a = store.get("Royal Caribbean", "A")
    assert a["deltas"] == {"Interior": -50.0} and a["drop"] == -50.0 and a["price_changed_on"] == "2026-01-03"
    assert a["min_price"] == 450.0
    assert store.get("Royal Caribbean", "B")["active"] == 0
    hist = store.class_history("Royal Caribbean", "A")
    assert [(h["room_class"], h["price"]) for h in hist if h["observed_on"] == "2026-01-03"] == [("Interior", 450.0)]


def test_search_filters_and_sorts(tmp_path):
    store = CatalogStore(tmp_path)
    store.upsert("Royal Caribbean", [
        sailing("A", SOON, Interior=500.0, Balcony=700.0),
        sailing("B", LATER, ship="Icon of the Seas", Interior=900.0, Balcony=650.0),
    ])
    rows, total = store.search(sort="price")
    assert total == 2 and [r["sailing_key"] for r in rows] == ["A", "B"]
    rows, _ = store.search(room_class="Balcony", max_price=680)
    assert [r["sailing_key"] for r in rows] == ["B"]
    rows, _ = store.search(ship="icon")
    assert [r["sailing_key"] for r in rows] == ["B"]
    rows, _ = store.search(port="Nassau", date_to=SOON)
    assert [r["sailing_key"] for r in rows] == ["A"]
    assert store.search(nights_min=5)[1] == 0
    assert "Interior" in store.room_classes()


def test_room_history_only_records_changes(tmp_path):
    store = CatalogStore(tmp_path)
    store.upsert("Royal Caribbean", [sailing("A")])
    store.watch("Royal Caribbean", "A")
    research = FakeLine([], [room("Infinite Balcony", "Balcony", 800.0), room("Interior", "Interior", 500.0, "4V")]).research
    req = type("R", (), {"ship": "UT", "sail_date": SOON})
    assert store.record_rooms("Royal Caribbean", "A", research(req), today="2026-01-01") == 2
    assert store.record_rooms("Royal Caribbean", "A", research(req), today="2026-01-02") == 0
    cheaper = FakeLine([], [room("Infinite Balcony", "Balcony", 760.0), room("Interior", "Interior", 500.0, "4V")])
    assert store.record_rooms("Royal Caribbean", "A", cheaper.research(req), today="2026-01-03") == 1
    latest = {r["room"]: r for r in store.latest_rooms("Royal Caribbean", "A")}
    assert latest["Infinite Balcony"]["price"] == 760.0 and latest["Infinite Balcony"]["prev_price"] == 800.0
    assert "Interior (4V)" in latest  # code added to keep same-named room types apart


def test_sync_keeps_going_when_a_line_fails(tmp_path):
    store = CatalogStore(tmp_path)
    good = FakeLine([sailing("A")], [room("Infinite Balcony", "Balcony", 800.0)])
    results = {r.line: r for r in sync_catalog(store, [good, BrokenLine([])])}
    assert results["Royal Caribbean"].sailings == 1 and results["Royal Caribbean"].error is None
    assert "site down" in results["Carnival"].error
    stats = {s["line"]: s for s in store.stats()}
    assert stats["Royal Caribbean"]["active"] == 1 and stats["Carnival"]["last_run"]["error"]

    store.watch("Royal Caribbean", "A")
    assert refresh_watched(store, [good]) == 1
    assert good.research_calls[0].ship == "Utopia of the Seas"  # ship name, which every provider accepts
    with pytest.raises(ValueError):
        refresh_rooms(store, [], "Royal Caribbean", "A")


def test_search_and_sailing_pages(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path, demo_mode=True, app_password="", anthropic_api_key="")
    line = FakeLine([sailing("UT4_X")], [room("Infinite Balcony", "Balcony", 800.0)])
    client = TestClient(create_app(settings, direct_providers=[line]))
    assert "No catalog yet" in client.get("/search").text

    CatalogStore(tmp_path).upsert("Royal Caribbean", [sailing("UT4_X")])
    page = client.get("/search?room_class=Balcony&sort=price").text
    assert "Utopia of the Seas" in page and "$700.00" in page and "1 sailing " in page
    assert "/quotes/new?cruise_line=Royal%20Caribbean&ship=Utopia%20of%20the%20Seas" in page

    assert client.post("/sailings/Royal Caribbean/UT4_X/watch", follow_redirects=False).status_code == 303
    detail = client.get("/sailings/Royal Caribbean/UT4_X").text
    assert "Infinite Balcony" in detail and "Stop watching" in detail
    assert client.get("/sailings/Royal Caribbean/nope").status_code == 404

    prefilled = client.get("/quotes/new?cruise_line=Viking&ship=Viking+Mira&sail_date=2026-10-14").text
    assert 'value="Viking Mira"' in prefilled and 'value="2026-10-14"' in prefilled
