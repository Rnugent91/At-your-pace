import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.msc import MSCProvider, cruise_id_from, ship_code_for

# Trimmed responses captured live from msccruisesusa.com's APIs (MSC World America, 2027-03-06).
FIX = json.loads((Path(__file__).parent / "fixtures" / "msc" / "am20270306.json").read_text())


def route(url: str):
    """Fake MSC backend: returns (status, json) for a request URL."""
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.netloc == "services.msccruises.com":
        return (200, FIX["itinerary"]) if "AM20270306MIAMIA" in u.path else (404, None)
    if "departureDateFrom" in q:
        hits = [h for h in FIX["search_ship_date"]["hits"]
                if h["cruiseID"].startswith(q.get("ship", "")) and h["departureStartDate"] == q["departureDateFrom"]]
        return 200, {"hits": hits}
    if q.get("includeFacets") == "true":
        return 200, FIX["cruise_facets"]
    if "macroCategory" in q:
        return 200, FIX["fares"].get(f"{q['macroCategory']}|{q.get('priceTypes', '')}", {"hits": []})
    return 200, {"hits": []}


def http_client(blocked=False, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if blocked:
            return httpx.Response(401, text="<html>Unauthorized</html>")
        status, body = route(str(request.url))
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeCF(CloudflareBrowser):
    """Runs the provider's in-page fetch script against the fake backend."""

    def __init__(self):
        super().__init__("acct", "token")
        self.calls = []

    def content(self, payload, attempts=3):
        self.calls.append(payload)
        script = payload["addScriptTag"][0]["content"]
        urls = json.loads(re.search(r"var urls=(\[.*?\]);", script).group(1))
        out = []
        for u in urls:
            status, body = route(u)
            out.append(json.dumps(body) if status == 200 else None)
        return f"<html><body><div id='qp-msc' data-json='{quote(json.dumps(out))}'></div></body></html>"


def provider(cf=None, **kw):
    return MSCProvider(cf or CloudflareBrowser("", ""), http=http_client(**kw))


def test_ship_codes_and_cruise_ids():
    assert ship_code_for("MSC World America") == "AM"
    assert ship_code_for("world america") == "AM"
    assert ship_code_for("Seascape") == "SC"
    assert ship_code_for(" sx ") == "SX"
    assert ship_code_for("Carnival Jubilee") is None
    url = "https://www.msccruisesusa.com/Booking?CruiseID=AM20270306MIAMI1&Category=IB"
    assert cruise_id_from(url) == "AM20270306MIAMI1"
    assert cruise_id_from("", "am20270306miamia") == "AM20270306MIAMIA"
    assert cruise_id_from("https://example.com") is None


def test_pick_sailing_prefers_standard_cruise_or_explicit_id():
    hits = FIX["search_ship_date"]["hits"]
    pick, others = MSCProvider.pick_sailing(hits, "AM", "2027-03-06")
    assert pick["cruiseID"] == "AM20270306MIAMIA" and pick["numberOfNights"] == 7
    assert [o["cruiseID"] for o in others] == ["AM20270306MIAMI1"]
    pick, _ = MSCProvider.pick_sailing(hits, "AM", "2027-03-06", "AM20270306MIAMI1")
    assert pick["numberOfNights"] == 14
    assert MSCProvider.pick_sailing(hits, "AM", "2027-03-07") == (None, [])


def test_full_research_direct():
    seen = []
    r = provider(seen=seen).research(ResearchRequest("MSC Cruises", "MSC World America", "2027-03-06"))
    assert r.ship == "MSC World America" and r.nights == 7 and r.currency == "USD"
    assert r.departure_port == "Miami, Florida" and r.itinerary_name == "Eastern Caribbean & Bahamas"
    # Every request carries the browser header set Akamai wants.
    assert all(req.headers.get("sec-fetch-mode") == "cors" for req in seen)

    ports = [d.port for d in r.itinerary]
    assert ports[0] == "Miami, Florida" and ports[1] == "At Sea" and len(ports) == 8
    assert (r.itinerary[0].arrive, r.itinerary[0].depart) == (None, "18:00")
    assert (r.itinerary[1].arrive, r.itinerary[1].depart) == (None, None)  # placeholder sea-day times dropped
    assert (r.itinerary[2].arrive, r.itinerary[2].depart) == ("09:00", "17:00")
    assert r.itinerary[-1].date == "2027-03-13" and r.itinerary[-1].depart is None

    rooms = {s.name: s for s in r.staterooms}
    assert list(rooms) == ["Interior", "Ocean View", "Balcony", "MSC Yacht Club"]
    interior = rooms["Interior"]
    # MSC's $764 pp includes $121 taxes & fees; we split them so the quote total matches.
    assert (interior.price_per_person, interior.taxes_fees_per_person) == (643.0, 121.0)
    assert interior.code == "IB" and not interior.sold_out
    assert "$1,156" in interior.notes and "+$392" in interior.notes and "$80 onboard credit" in interior.notes
    yc = rooms["MSC Yacht Club"]
    assert yc.category == "Suite" and yc.price_per_person + yc.taxes_fees_per_person == 3574
    assert "already included" in yc.notes

    (drinks,) = r.addons
    assert drinks.kind == "beverage" and drinks.price == 392 and drinks.price_unit == "per_person"
    assert "Yacht Club" in drinks.description and "only available when booking" in drinks.description

    assert any("Suite" in w and "sold out" in w for w in r.warnings)
    assert any("AM20270306MIAMI1" in w for w in r.warnings)
    assert r.sources[0] == "https://www.msccruisesusa.com/Booking?CruiseID=AM20270306MIAMIA"


def test_blocked_direct_falls_back_to_cloudflare():
    cf = FakeCF()
    r = provider(cf, blocked=True).research(ResearchRequest("MSC", "AM", "2027-03-06", adults=3))
    assert len(cf.calls) == 3  # sailing lookup, itinerary+facets, fares
    assert cf.calls[0]["url"] == "https://www.msccruisesusa.com/"
    assert len(r.staterooms) == 4 and r.addons[0].price == 392 and len(r.itinerary) == 8
    assert any("based on 2 guests" in w for w in r.warnings)


def test_blocked_without_cloudflare_raises():
    with pytest.raises(ProviderError, match="CLOUDFLARE"):
        provider(blocked=True).research(ResearchRequest("MSC", "MSC World America", "2027-03-06"))


def test_unknown_ship_and_missing_date_raise():
    with pytest.raises(ProviderError):
        provider().research(ResearchRequest("MSC", "MSC Nonexistent", "2027-03-06"))
    with pytest.raises(ProviderError):
        provider().research(ResearchRequest("MSC", "MSC World America", "2027-03-07"))


def test_varying_drinks_delta_is_reported_per_category():
    def hit(macro, ptype, desc, price):
        return {"cruiseID": "X", "macroCategory": {"key": macro}, "category": {"key": macro[:2]},
                "priceType": ptype, "priceDesc": desc, "itemDesc": "",
                "prices": {"adultPrice": price, "portCharges": 100, "availability": True}}
    fares = {
        "INS": [hit("INS", "EZOB", "CRUISE ONLY OBC INCLUDED", 500), hit("INS", "EZPF", "CRUISE WITH DRINKS WIFI OBC", 800)],
        "BAL": [hit("BAL", "EZAT", "ESCAPE TO SEA CRUISE ONLY", 700), hit("BAL", "EZPF", "CRUISE WITH DRINKS WIFI OBC", 1050)],
    }
    rooms, deltas = MSCProvider.build_staterooms(fares, "src")
    assert deltas == {"INS": 300, "BAL": 350}
    assert [r.price_per_person for r in rooms] == [400, 600]
    addon = MSCProvider.drinks_addon(deltas, 7, "src")
    assert addon.price == 300 and "(from)" in addon.unit_label and "Balcony +$350" in addon.description
