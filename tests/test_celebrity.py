"""Celebrity provider tests. Fixtures are trimmed live responses captured Sept 2026
(Celebrity Beyond, 4-night Western Caribbean from Fort Lauderdale, 2026-11-18)."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.celebrity import SEARCH_URL, CelebrityProvider, ship_code_for

FIX = Path(__file__).parent / "fixtures" / "celebrity"
SEARCH = json.loads((FIX / "search_BY_2026-11-18.json").read_text())
PRODUCTS = json.loads((FIX / "products_BY_2026-11-18.json").read_text())
EMPTY_PRODUCTS = {"data": {"products": {}}}  # what Celebrity returns for empty categories


def make_transport(search=SEARCH, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append((str(request.url), dict(request.headers), body))
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json=search)
        return httpx.Response(200, json=PRODUCTS.get(body["variables"]["category"], EMPTY_PRODUCTS))

    return handler


def provider(cf=None, search=SEARCH, seen=None):
    http = httpx.Client(transport=httpx.MockTransport(make_transport(search, seen)))
    return CelebrityProvider(cf or CloudflareBrowser("", ""), "https://example.test/graphql", http=http)


def test_handles_and_ship_codes():
    p = provider()
    assert p.handles("Celebrity Cruises") and p.handles("celebrity") and not p.handles("Royal Caribbean")
    assert ship_code_for("Celebrity Beyond") == "BY"
    assert ship_code_for("beyond") == "BY"
    assert ship_code_for("Xcel") == "XC"
    assert ship_code_for(" eg ") == "EG"
    assert ship_code_for("UT") is None  # an RC code, not Celebrity
    assert ship_code_for("Utopia of the Seas") is None


def test_full_research_offline():
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

    # Search prices include taxes; the fare is split out.
    rooms = {s.name: s for s in r.staterooms}
    assert [(s.category, s.name) for s in r.staterooms] == [
        ("Interior", "Inside"),
        ("Ocean View", "Ocean View"),
        ("Balcony", "Veranda"),
        ("Balcony", "Concierge Class"),
        ("Balcony", "AquaClass"),
        ("Suite", "The Retreat (Suites)"),
    ]
    assert rooms["Inside"].price_per_person == 528.5 and rooms["Inside"].taxes_fees_per_person == 118.2
    assert rooms["The Retreat (Suites)"].price_per_person == 2266.0
    assert not any(s.sold_out for s in r.staterooms)

    # Bundles listed under both drinks and wifi appear once, as beverage.
    names = [a.name for a in r.addons]
    assert names.count("Ultimate Bundle: Premium Drinks and Wi-Fi Upgrade") == 1
    drinks = next(a for a in r.addons if a.name == "Premium Drinks Package")
    assert drinks.kind == "beverage" and drinks.price == 87.99 and drinks.price_unit == "per_person_per_day"
    assert drinks.source == "Celebrity Cruise Planner"
    wifi = next(a for a in r.addons if a.kind == "internet")
    assert wifi.name == "Premium Wi-Fi" and wifi.price_unit == "per_person_per_day"
    shorex = next(a for a in r.addons if a.kind == "excursion")
    assert shorex.port == "Cozumel, Mexico" and shorex.price_unit == "per_person"
    assert r.warnings == []

    search_url, headers, body = seen[0]
    assert search_url == SEARCH_URL and headers["brand"] == "C"
    assert body["variables"]["filters"] == "ship:BY|startDate:2026-11-18~2026-11-18"
    product_calls = [h for u, h, _ in seen if u != SEARCH_URL]
    assert product_calls and all(h["appkey"] == "qpRMO6lj4smwkT1sWlSdIj7b8QF5rG8Q" for h in product_calls)


def test_sold_out_class_and_party_size_warning():
    search = copy.deepcopy(SEARCH)
    sailing = search["data"]["cruiseSearch"]["results"]["cruises"][0]["sailings"][0]
    sailing["stateroomClassPricing"][3]["price"] = None
    r = provider(search=search).research(ResearchRequest("Celebrity", "BY", "2026-11-18", adults=3))
    concierge = next(s for s in r.staterooms if s.code == "CONCIERGE")
    assert concierge.sold_out and concierge.price_per_person is None
    assert any("party size" in w for w in r.warnings)


def test_unknown_ship_and_missing_date_raise():
    empty = {"data": {"cruiseSearch": {"results": {"cruises": []}}}}
    with pytest.raises(ProviderError):
        provider().research(ResearchRequest("Celebrity", "Celebrity Nowhere", "2026-11-18"))
    with pytest.raises(ProviderError):
        provider(search=empty).research(ResearchRequest("Celebrity", "Beyond", "2027-01-01"))


def test_search_failure_raises_provider_error():
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403, text="denied")))
    p = CelebrityProvider(CloudflareBrowser("", ""), "https://example.test/graphql", http=http)
    with pytest.raises(ProviderError):
        p.research(ResearchRequest("Celebrity", "Beyond", "2026-11-18"))


VERANDA_HTML = """
<div data-testid="guarantee-content-container">
  <h3 data-testid="card-title">Veranda - We Choose Your Room</h3>
  <div data-testid="room-details-price"><span data-testid="main-price-amount">$600</span></div>
</div>
<div class="Room_content__x">
  <h3 data-testid="card-title">Edge Stateroom with Infinite Veranda</h3>
</div>
<div class="Room_content__x">
  <h3 data-testid="card-title">Infinite Veranda</h3>
  <div data-testid="room-details-price"><span data-testid="main-price-amount">$640</span></div>
</div>
"""


class FakeCF(CloudflareBrowser):
    def __init__(self):
        super().__init__("acct", "token")
        self.calls = []

    def content(self, payload, attempts=3):
        self.calls.append(payload["url"])
        return VERANDA_HTML if "cabinClassType=BALCONY" in payload["url"] else "<html></html>"


def test_room_types_via_cloudflare_replace_class_row():
    cf = FakeCF()
    r = provider(cf=cf).research(ResearchRequest("Celebrity", "Beyond", "2026-11-18", adults=2, children=1))
    assert len(cf.calls) == 6
    url = next(u for u in cf.calls if "cabinClassType=BALCONY" in u)
    assert url.startswith("https://www.celebritycruises.com/room-selection/room-subtype")
    assert "groupId=BY04FLL-4187031086" in url and "packageCode=BY04W200" in url and "r0a=2&r0c=1" in url
    balcony = [s for s in r.staterooms if s.category == "Balcony"]
    assert [s.name for s in balcony] == ["Edge Stateroom with Infinite Veranda", "Infinite Veranda", "Concierge Class", "AquaClass"]
    infinite = next(s for s in balcony if s.name == "Infinite Veranda")
    assert infinite.price_per_person == 640 and infinite.taxes_fees_per_person == 118.2
    assert "celebritycruises.com room selection" in r.sources
    assert any(s.name == "Inside" for s in r.staterooms)  # other classes keep their lead-in row
