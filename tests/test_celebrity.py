"""Celebrity provider tests. Fixtures are trimmed live responses captured Sept 2026
(Celebrity Beyond, 4-night Western Caribbean from Fort Lauderdale, 2026-11-18)."""

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.celebrity import ROOMS_API, SEARCH_URL, CelebrityProvider, ship_code_for

FIX = Path(__file__).parent / "fixtures" / "celebrity"
SEARCH = json.loads((FIX / "search_BY_2026-11-18.json").read_text())
ROOMS = json.loads((FIX / "rooms_BY04W200_2026-11-18.json").read_text())
PRODUCTS = json.loads((FIX / "products_BY_2026-11-18.json").read_text())
EMPTY_PRODUCTS = {"data": {"products": {}}}  # what Celebrity returns for empty categories


def make_transport(search=SEARCH, rooms=ROOMS, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append((request.method, url, dict(request.headers), request.content))
        if url.startswith(ROOMS_API):
            return rooms if isinstance(rooms, httpx.Response) else httpx.Response(200, json=rooms)
        body = json.loads(request.content)
        if url == SEARCH_URL:
            return httpx.Response(200, json=search(body) if callable(search) else search)
        return httpx.Response(200, json=PRODUCTS.get(body["variables"]["category"], EMPTY_PRODUCTS))

    return handler


def provider(cf=None, seen=None, **kw):
    http = httpx.Client(transport=httpx.MockTransport(make_transport(seen=seen, **kw)))
    return CelebrityProvider(cf or CloudflareBrowser("", ""), "https://example.test/graphql", http=http)


def test_handles_and_ship_codes():
    p = provider()
    assert p.handles("Celebrity Cruises") and p.handles("celebrity") and not p.handles("Royal Caribbean")
    assert p.cruise_line == "Celebrity"
    assert ship_code_for("Celebrity Beyond") == "BY"
    assert ship_code_for("beyond") == "BY"
    assert ship_code_for("Xcel") == "XC"
    assert ship_code_for(" eg ") == "EG"
    assert ship_code_for("UT") is None  # an RC code, not Celebrity
    assert ship_code_for("Utopia of the Seas") is None


def test_full_research_with_room_types():
    seen = []
    r = provider(seen=seen).research(ResearchRequest("Celebrity", "Celebrity Beyond", "2026-11-18"))
    assert r.cruise_line == "Celebrity" and r.ship == "Celebrity Beyond" and r.nights == 4
    assert r.departure_port == "Fort Lauderdale, Florida" and r.itinerary_name == "Western Caribbean"
    assert [(d.port, d.arrive, d.depart) for d in r.itinerary] == [
        ("Fort Lauderdale, Florida", None, "16:00"),
        ("At Sea", None, None),
        ("Cozumel, Mexico", "07:00", "20:00"),
        ("At Sea", None, None),
        ("Fort Lauderdale, Florida", "07:00", None),
    ]
    assert r.itinerary[2].date == "2026-11-20"

    # One row per room type, in class order, fare excl. taxes per person.
    assert [(s.category, s.code) for s in r.staterooms] == [
        ("Interior", "I2"), ("Ocean View", "Y"), ("Ocean View", "O2"),
        ("Balcony", "EX"), ("Balcony", "P2"), ("Balcony", "E2"),
        ("Balcony", "XC"), ("Balcony", "XA"), ("Suite", "S1"), ("Suite", "SS"),
    ]
    rooms = {s.code: s for s in r.staterooms}
    edge = rooms["E2"]
    assert edge.name == "Edge Stateroom with Infinite Veranda (E2)"
    assert edge.price_per_person == 756.5 and edge.taxes_fees_per_person == 118.2
    assert "10 left" in edge.notes and not edge.sold_out
    assert rooms["I2"].name == "Inside Stateroom (Guarantee, I2)" and rooms["I2"].price_per_person == 528.5
    assert "Guarantee" in rooms["I2"].notes
    assert rooms["I2"].source.startswith("https://www.celebritycruises.com/room-selection/rooms-and-guests?groupId=BY04FLL")

    # Rooms API was asked for the real party with all options.
    method, url, headers, _ = next(s for s in seen if s[1].startswith(ROOMS_API))
    filt = json.loads(parse_qs(urlparse(url).query)["filter"][0])
    assert method == "GET" and headers["brand"] == "C"
    assert filt["packageId"] == "BY04W200" and filt["options"] is True
    assert filt["rooms"] == [{"adultCount": 2, "childCount": 0}]

    # Bundles listed under both drinks and wifi appear once, as beverage.
    names = [a.name for a in r.addons]
    assert names.count("Ultimate Bundle: Premium Drinks and Wi-Fi Upgrade") == 1
    drinks = next(a for a in r.addons if a.name == "Premium Drinks Package")
    assert drinks.kind == "beverage" and drinks.price == 87.99 and drinks.price_unit == "per_person_per_day"
    assert drinks.source == "Celebrity Cruise Planner"
    wifi = next(a for a in r.addons if a.kind == "internet")
    assert wifi.name == "Premium Wi-Fi" and wifi.price_unit == "per_person_per_day"
    assert next(a for a in r.addons if a.kind == "excursion").port == "Cozumel, Mexico"
    assert r.warnings == []

    _, search_url, search_headers, body = seen[0]
    assert search_url == SEARCH_URL and search_headers["brand"] == "C"
    assert json.loads(body)["variables"]["filters"] == "ship:BY|startDate:2026-11-18~2026-11-18"
    product_calls = [h for _, u, h, _ in seen if u not in (SEARCH_URL,) and not u.startswith(ROOMS_API)]
    assert product_calls and all(h["appkey"] == "qpRMO6lj4smwkT1sWlSdIj7b8QF5rG8Q" for h in product_calls)


def test_party_of_three_prices_average_and_missing_classes_sold_out():
    rooms = copy.deepcopy(ROOMS)
    types = rooms["rooms"][0]["options"]["stateroomTypes"]
    rooms["rooms"][0]["options"]["stateroomTypes"] = [t for t in types if t["code"] != "INTERIOR"]
    r = provider(rooms=rooms).research(ResearchRequest("Celebrity", "BY", "2026-11-18", adults=2, children=1))
    inside = next(s for s in r.staterooms if s.category == "Interior")
    assert inside.sold_out and inside.price_per_person is None and "3 guests" in inside.notes
    edge = next(s for s in r.staterooms if s.code == "E2")
    assert edge.price_per_person == round(1513 / 3, 2) and "Average per guest" in edge.notes


def test_rooms_api_blocked_falls_back_to_class_fares():
    r = provider(rooms=httpx.Response(403, text="Access Denied")).research(
        ResearchRequest("Celebrity", "Beyond", "2026-11-18")
    )
    assert [(s.category, s.name) for s in r.staterooms] == [
        ("Interior", "Inside"),
        ("Ocean View", "Ocean View"),
        ("Balcony", "Veranda"),
        ("Balcony", "Concierge Class"),
        ("Balcony", "AquaClass"),
        ("Suite", "The Retreat (Suites)"),
    ]
    inside = r.staterooms[0]
    assert inside.price_per_person == 528.5 and inside.taxes_fees_per_person == 118.2  # 646.70 incl. taxes
    assert any("room-type prices couldn't be loaded" in w for w in r.warnings)


class FakeCF(CloudflareBrowser):
    """Answers in-page fetches the way the injected script would: JSON in #qp-json."""

    def __init__(self, payload):
        super().__init__("acct", "token")
        self.calls = []
        self.payload = payload

    def content(self, payload, attempts=3):
        self.calls.append(payload)
        data = quote(json.dumps(self.payload))
        return f"<html><body><div id='qp-json' data-json='{data}'></div></body></html>"


def test_rooms_api_via_cloudflare_when_blocked():
    cf = FakeCF(ROOMS)
    r = provider(cf=cf, rooms=httpx.Response(403, text="Access Denied")).research(
        ResearchRequest("Celebrity", "Beyond", "2026-11-18")
    )
    assert any(s.code == "E2" for s in r.staterooms)
    (call,) = cf.calls
    assert call["url"].startswith("https://www.celebritycruises.com/room-selection/rooms-and-guests?groupId=BY04FLL")
    script = call["addScriptTag"][0]["content"]
    assert "/room-selection/api/v1/rooms?filter=" in script and '"brand": "C"' in script


def test_unknown_ship_and_missing_date_raise():
    empty = {"data": {"cruiseSearch": {"results": {"total": 0, "cruises": []}}}}
    with pytest.raises(ProviderError):
        provider().research(ResearchRequest("Celebrity", "Celebrity Nowhere", "2026-11-18"))
    with pytest.raises(ProviderError):
        provider(search=empty).research(ResearchRequest("Celebrity", "Beyond", "2027-01-01"))


def test_search_failure_raises_provider_error():
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403, text="denied")))
    p = CelebrityProvider(CloudflareBrowser("", ""), "https://example.test/graphql", http=http)
    with pytest.raises(ProviderError):
        p.research(ResearchRequest("Celebrity", "Beyond", "2026-11-18"))


def test_iter_catalog_pages_whole_line():
    cruise = SEARCH["data"]["cruiseSearch"]["results"]["cruises"][0]
    second = copy.deepcopy(cruise)
    second["id"] = "BY04FLL-999"
    second["sailings"][0]["id"] = "BY04W201_2026-11-22"
    second["sailings"][0]["sailDate"] = "2026-11-22"
    second["sailings"][0]["stateroomClassPricing"][5]["price"] = None  # Retreat sold out
    pages = [cruise, second]
    calls = []

    def search(body):
        v = body["variables"]
        calls.append(v)
        skip = v["pagination"]["skip"]
        return {"data": {"cruiseSearch": {"results": {"total": 2, "cruises": pages[skip:skip + v["pagination"]["count"]]}}}}

    rows = list(provider(search=search).iter_catalog(page_size=1))
    assert [c["filters"] for c in calls] == ["", ""] and len(calls) == 2
    assert [r.sailing_key for r in rows] == ["BY04W200_2026-11-18", "BY04W201_2026-11-22"]
    row = rows[0]
    assert row.cruise_line == "Celebrity" and row.ship == "Celebrity Beyond" and row.ship_code == "BY"
    assert row.nights == 4 and row.departure_port == "Fort Lauderdale, Florida" and row.ports == ["Cozumel, Mexico"]
    assert row.prices == {
        "Interior": 528.5, "Ocean View": 511.0, "Balcony": 611.0,
        "Concierge Class": 661.0, "AquaClass": 1161.0, "Suite": 2266.0,
    }
    assert row.taxes_fees_per_person == 118.2
    assert row.booking_url == (
        "https://www.celebritycruises.com/room-selection/rooms-and-guests?groupId=BY04FLL-4187031086"
        "&packageCode=BY04W200&sailDate=2026-11-18&shipCode=BY&country=USA&selectedCurrencyCode=USD"
    )
    assert rows[1].prices["Suite"] is None
