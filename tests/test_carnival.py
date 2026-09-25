"""Carnival provider tests. Fixtures are trimmed live carnival.com responses for
Carnival Vista, 6-day Eastern Caribbean from Port Canaveral, sailing 2027-03-28."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.carnival import CarnivalProvider, mmddyyyy, port_slugs, ship_code_for

FIX = Path(__file__).parent / "fixtures" / "carnival"


def load(name):
    return json.loads((FIX / name).read_text())


def make_transport(calls, book_fail=("OB",)):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/cruisesearch/api/search":
            return httpx.Response(200, json=load("search.json"))
        if path == "/cruisesearch/api/search/itinerary":
            return httpx.Response(200, json=load("itinerary.json"))
        if path == "/booking-api/api/v1.0/book":
            meta = json.loads(request.content)["cabins"][0]["metaCode"] or "IS"
            if meta in book_fail:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=load(f"book_{meta}.json"))
        if path.startswith("/shop/plp/search/"):
            slug = path.rsplit("/", 1)[1]
            f = FIX / f"shop_{slug}.json"
            if f.exists():
                return httpx.Response(200, json=load(f.name))
            if slug == "celebration-key":  # a slug that answers with another port's excursions
                data = load("shop_amber-cove.json")
                data["categoryName"] = "XXX"
                return httpx.Response(200, json=data)
            return httpx.Response(200, json={"productCategorySearchPageData": None, "categoryName": None})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def provider(calls, **kw):
    p = CarnivalProvider(http=httpx.Client(transport=make_transport(calls, **kw)))
    p._sleep = lambda s: None  # retries/backoff without waiting
    p._today = lambda: date(2026, 9, 25)
    return p


REQ = ResearchRequest(cruise_line="Carnival Cruise Line", ship="Carnival Vista", sail_date="2027-03-28")


def test_helpers():
    assert ship_code_for("Carnival Vista") == "VS"
    assert ship_code_for("vista") == "VS"
    assert ship_code_for("Mardi Gras") == "MD"
    assert ship_code_for("jb") == "JB"
    assert ship_code_for("Queen Mary") is None
    assert mmddyyyy("2027-03-28") == "03282027"
    assert port_slugs("RelaxAway, Half Moon Cay™") == ["relaxaway-half-moon-cay", "relaxaway", "half-moon-cay"]
    assert "port-canaveral-orlando" in port_slugs("Port Canaveral (Orlando)")
    assert CarnivalProvider(http=httpx.Client()).handles("Carnival Cruise Line")
    assert not CarnivalProvider(http=httpx.Client()).handles("Royal Caribbean")


def test_research_end_to_end():
    calls = []
    r = provider(calls).research(REQ)

    assert r.cruise_line == "Carnival" and r.ship == "Carnival Vista"
    assert r.nights == 6
    assert r.departure_port == "Port Canaveral (Orlando), FL"
    assert r.itinerary_name.startswith("6-Day Eastern Caribbean")

    # The search's lead sailing is 2027-03-14, so the exact schedule comes from the itinerary API.
    itin_call = next(c for c in calls if c.url.path.endswith("/itinerary"))
    assert parse_qs(itin_call.url.query.decode())["sailDate"] == ["03282027"]
    search_call = next(c for c in calls if c.url.path == "/cruisesearch/api/search")
    q = parse_qs(search_call.url.query.decode())
    assert q["shipCode"] == ["VS"] and q["datFrom"] == ["032027"]

    days = r.itinerary
    assert [d.port for d in days][:3] == ["Port Canaveral (Orlando)", "At Sea", "Amber Cove"]
    assert days[0].date == "2027-03-28" and days[0].depart == "15:30" and days[0].arrive is None
    assert (days[2].arrive, days[2].depart) == ("10:30", "18:30")
    assert days[4].port == "RelaxAway, Half Moon Cay"  # trademark sign stripped
    assert days[-1].date == "2027-04-03" and days[-1].arrive == "08:00"

    rooms = {(s.category, s.name): s for s in r.staterooms}
    # Carnival's shown prices are all-in ($606); taxes, fees & port expenses are
    # $97.23 + $85.77 = $183 per person, so the fare is $423.
    ul = rooms[("Interior", "Interior Upper/Lower")]
    assert ul.code == "VSULUL" and ul.price_per_person == 423 and ul.taxes_fees_per_person == 183
    gtee = rooms[("Interior", "Interior (guarantee)")]
    assert gtee.price_per_person == 454 and "assigns" in gtee.notes
    assert rooms[("Suite", "Ocean Suite")].price_per_person == 1428
    assert any(s.category == "Ocean View" and s.code.startswith("VSOS") for s in r.staterooms)
    # The Balcony call failed (500): keep the room type's "from" price rather than dropping it.
    bal = [s for s in r.staterooms if s.category == "Balcony"]
    assert len(bal) == 1 and bal[0].price_per_person == 977 - 183 and bal[0].code == "OB"
    # Cheapest room type first.
    assert r.staterooms[0].category == "Interior" and r.staterooms[-1].category == "Suite"
    # One booking-flow call per room type, reusing the first response for the default type.
    metas = [json.loads(c.content)["cabins"][0]["metaCode"] for c in calls if c.url.path.endswith("/book")]
    assert set(metas) == {None, "OS", "OB", "SU"}
    assert metas.count("OS") == 1 and metas.count("OB") == 4  # the failing call was retried

    add = {(a.kind, a.name): a for a in r.addons}
    cheers = add[("beverage", "CHEERS!")]
    assert cheers.price == 69.95 and cheers.price_unit == "per_person_per_day"
    assert "service charge" in cheers.description
    assert add[("beverage", "Cruise the Vineyard Premium Wine Package")].price_unit == "flat"
    assert add[("internet", "Premium Wi-Fi Plan")].price == 25.5
    assert add[("internet", "Multi-Device Premium Wi-Fi Plan - Up to 4 Devices")].price_unit == "per_device_per_day"
    exc = [a for a in r.addons if a.kind == "excursion"]
    assert {a.port for a in exc} == {"Amber Cove", "RelaxAway, Half Moon Cay"}
    first = exc[0]
    assert first.price_unit == "per_person" and first.source.startswith("https://www.carnival.com/shore-excursions/")
    assert "child from $" in first.description
    # The celebration-key slug answered with another port's results, so it was rejected.
    assert any("Celebration Key" in w for w in r.warnings)
    assert any("'from' prices" in w for w in r.warnings)


def test_book_request_body():
    calls = []
    provider(calls).research(ResearchRequest(cruise_line="Carnival", ship="VS", sail_date="2027-03-28",
                                             adults=2, children=1))
    body = json.loads(next(c for c in calls if c.url.path.endswith("/book")).content)
    assert body["sailingId"] == 22186
    assert body["sailDate"] == "2027-03-28" and body["shipCode"] == "VS" and body["durationDays"] == 6
    assert body["cabins"][0]["qualifiers"]["numberOfGuests"] == 3


def test_booking_flow_down_falls_back_to_search_prices():
    calls = []
    r = provider(calls, book_fail=("IS", "OS", "OB", "SU")).research(REQ)
    cats = {s.category: s for s in r.staterooms}
    assert set(cats) == {"Interior", "Ocean View", "Balcony", "Suite"}
    assert all(s.name.endswith("(lowest fare)") for s in r.staterooms)
    assert all(s.price_per_person for s in r.staterooms)
    assert cats["Interior"].price_per_person == 606  # all-in: no tax breakdown without the booking API
    assert any("room picker failed" in w and "INCLUDE taxes" in w for w in r.warnings)
    assert r.itinerary  # the rest still works


def test_unknown_ship_and_missing_sailing():
    with pytest.raises(ProviderError):
        provider([]).research(ResearchRequest(cruise_line="Carnival", ship="Nonexistent", sail_date="2027-03-28"))
    with pytest.raises(ProviderError, match="No Carnival sailing"):
        provider([]).research(ResearchRequest(cruise_line="Carnival", ship="Vista", sail_date="2027-03-29"))


def test_wrong_ship_code_detected():
    with pytest.raises(ProviderError, match="not"):
        provider([]).research(ResearchRequest(cruise_line="Carnival", ship="Mardi Gras", sail_date="2027-03-28"))


class FakeCF(CloudflareBrowser):
    def __init__(self):
        super().__init__("acct", "token")
        self.calls = []

    def content(self, payload, attempts=3):
        self.calls.append(payload)
        script = payload["addScriptTag"][0]["content"]
        path = json.loads(script.split("fetch(", 1)[1].split(",", 1)[0])
        u = urlparse(path)
        if u.path == "/cruisesearch/api/search":
            data = load("search.json")
        elif u.path.endswith("/itinerary"):
            data = load("itinerary.json")
        elif u.path.endswith("/book"):
            data = load("book_IS.json")
        else:
            data = {"productCategorySearchPageData": {"results": []}}
        return f"<html><body><div id='qp-carnival' data-json='{quote(json.dumps(data))}'></div></body></html>"


def test_blocked_server_ip_uses_cloudflare_browser():
    direct = []

    def blocked(request):
        direct.append(request)
        return httpx.Response(403, text="Access Denied")

    cf = FakeCF()
    p = CarnivalProvider(cf=cf, http=httpx.Client(transport=httpx.MockTransport(blocked)))
    r = p.research(REQ)
    assert len(direct) == 1  # after the first 403 everything goes through the browser
    assert cf.calls and all("#qp-carnival" == c["waitForSelector"]["selector"] for c in cf.calls)
    book_script = next(c for c in cf.calls if "/booking-api/" in c["addScriptTag"][0]["content"])
    assert '"method": "POST"' in book_script["addScriptTag"][0]["content"]
    assert r.itinerary and r.staterooms


def test_blocked_without_cloudflare_raises():
    p = CarnivalProvider(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))))
    with pytest.raises(ProviderError):
        p.research(REQ)


def test_taxes_and_fare_helpers():
    resp = {"cabins": [{"guestPrices": [
        {"price": 704.56, "cruiseFeesAndExpenses": 100.56, "taxesAndFees": 94.44},
        {"price": 704.56, "cruiseFeesAndExpenses": 100.56, "taxesAndFees": 94.44},
    ]}]}
    assert CarnivalProvider.taxes_from_book(resp) == 195
    assert CarnivalProvider.taxes_from_book({"cabins": [{}]}) is None
    assert CarnivalProvider.fare(564, 195) == 369
    assert CarnivalProvider.fare(564, None) == 564
    assert CarnivalProvider.fare(0, 195) is None


def test_retries_429_then_succeeds():
    n = {"calls": 0}

    def flaky(request):
        n["calls"] += 1
        if n["calls"] < 3:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json={"ok": True})

    p = CarnivalProvider(http=httpx.Client(transport=httpx.MockTransport(flaky)))
    slept = []
    p._sleep = slept.append
    assert p._json("GET", "/x") == {"ok": True}
    assert slept == [1.0, 1.0] and p.http_calls == 3


def catalog_transport(calls, fail_book=False):
    inner = make_transport(calls)

    def handler(request):
        if request.url.path == "/cruisesearch/api/search":
            q = parse_qs(request.url.query.decode())
            if "shipCode" not in q:
                calls.append(request)
                return httpx.Response(200, json={"options": {"shipCode": [{"code": "VS"}, {"code": "MD"}]},
                                                 "results": {"itineraries": [], "lastPage": 1}})
            if q["shipCode"] == ["MD"]:
                calls.append(request)
                return httpx.Response(200, json={"results": {"itineraries": [], "lastPage": 1}})
        if fail_book and request.url.path.endswith("/book"):
            calls.append(request)
            return httpx.Response(500)
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def test_iter_catalog():
    calls = []
    p = CarnivalProvider(http=httpx.Client(transport=catalog_transport(calls)))
    p._sleep = lambda s: None
    p._today = lambda: date(2027, 3, 10)  # the 2027-03-06 sailing has already left
    rows = list(p.iter_catalog())
    search = load("search.json")
    expected = {s["sailingId"] for i in search["results"]["itineraries"] for s in i["sailings"]
                if s["departureDate"][:10] >= "2027-03-10"}
    assert {r.sailing_key for r in rows} == expected
    row = next(r for r in rows if r.sail_date == "2027-03-28")
    assert row.cruise_line == "Carnival" and row.ship == "Carnival Vista" and row.ship_code == "VS"
    assert row.nights == 6 and row.departure_port == "Port Canaveral (Orlando), FL"
    assert row.ports == ["Amber Cove", "RelaxAway, Half Moon Cay", "Celebration Key"]
    assert row.booking_url.startswith("https://www.carnival.com/booking?") and "sailingID=22186" in row.booking_url
    sailing = next(s for i in search["results"]["itineraries"] for s in i["sailings"] if s["sailingId"] == "22186")
    assert row.taxes_fees_per_person == 183
    assert row.prices["Interior"] == sailing["rooms"]["interior"]["price"] - 183
    assert set(row.prices) == {"Interior", "Ocean View", "Balcony", "Suite"}
    # One booking call per itinerary (ship + route), not per sailing.
    books = [c for c in calls if c.url.path.endswith("/book")]
    assert len(books) == 2  # CEF and CEG itineraries still have future sailings
    assert p.http_calls == len(calls)


def test_iter_catalog_per_sailing_and_tax_failure():
    calls = []
    p = CarnivalProvider(http=httpx.Client(transport=catalog_transport(calls)))
    p._sleep = lambda s: None
    p._today = lambda: date(2026, 9, 25)
    rows = list(p.iter_catalog(taxes="sailing"))
    assert len([c for c in calls if c.url.path.endswith("/book")]) == len(rows) == 4

    calls = []
    p = CarnivalProvider(http=httpx.Client(transport=catalog_transport(calls, fail_book=True)))
    p._sleep = lambda s: None
    p._today = lambda: date(2026, 9, 25)
    rows = list(p.iter_catalog())
    # Without the tax breakdown the prices stay all-in, flagged by taxes_fees_per_person=None.
    assert rows and all(r.taxes_fees_per_person is None for r in rows)
    r = next(r for r in rows if r.sailing_key == "22186")
    assert r.prices["Interior"] == 606
    with pytest.raises(ValueError):
        next(p.iter_catalog(taxes="bogus"))
