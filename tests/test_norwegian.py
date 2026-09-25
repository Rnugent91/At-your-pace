"""Norwegian (NCL) provider tests. Fixtures are trimmed captures of ncl.com's JSON APIs
(Sept 2026, Norwegian Getaway 3-night Bahamas); nothing here touches the network."""

import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.norwegian import (
    NorwegianProvider,
    ordered_ports,
    parse_booking_url,
    parse_schedule,
    ship_code_for,
)

FIX = Path(__file__).parent / "fixtures" / "norwegian"
CODE = "GETAWAY3MIANASNPIMIA"
PKG = "24223987"


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class NCLSite:
    """A fake ncl.com backed by the fixtures; records every request."""

    def __init__(self, status: int = 200, fail_availability: bool = False, fail_summary_for: tuple = ()):
        self.status = status
        self.fail_availability = fail_availability
        self.fail_summary_for = set(fail_summary_for)
        self.requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="Access Denied")
        path = request.url.path
        if path == "/api/v2/vacations/search":
            return httpx.Response(200, json=fx("search_getaway_may2027.json"))
        if path.startswith("/api/vacations/sailings/"):
            code = path.rsplit("/", 1)[1]
            if code == CODE:
                return httpx.Response(200, json=fx("sailings_GETAWAY3MIANASNPIMIA.json"))
            return httpx.Response(200, json={"pricingStateRooms": [], "itineraryDetails": {}, "itineraryCode": code})
        if path == f"/api/vacation-builder/itinerary/{CODE}/package/{PKG}/events":
            return httpx.Response(200, json=fx("events_GETAWAY3MIANASNPIMIA_24223987.json"))
        if path == "/api/vacation-builder/v2/stateroom-types-availability":
            if self.fail_availability:
                return httpx.Response(200, json={"results": [{"stateroomId": "0", "error": {"success": False, "error": {
                    "code": "Unknown_ERROR", "message": "Failed to get the stateroom types availability"}}}]})
            assert json.loads(request.content)["sailingFilters"][0]["packageId"] == PKG
            return httpx.Response(200, json=fx("availability_24223987.json"))
        if path == "/api/vacation-builder/price-summary":
            body = json.loads(request.content)
            if body["packageId"] in self.fail_summary_for:
                return httpx.Response(404, json=[{"errors": [{"code": "HARD_STOP"}]}])
            f = body["stateroomFilters"][0]
            name = "price_summary_24223987_HI.json" if f["stateroomTypeCode"] == "HAVEN" else "price_summary_24223987_IX.json"
            return httpx.Response(200, json=fx(name))
        return httpx.Response(404, json={"message": "not found"})


def provider(site: NCLSite, cf=None, **kw) -> NorwegianProvider:
    p = NorwegianProvider(cf or CloudflareBrowser("", ""), http=httpx.Client(transport=httpx.MockTransport(site.handler)), **kw)
    p._sleep = lambda s: None
    return p


# ── helpers ─────────────────────────────────────────────────────────────


def test_handles():
    p = provider(NCLSite())
    assert p.handles("Norwegian") and p.handles("Norwegian Cruise Line") and p.handles("NCL")
    assert not p.handles("Carnival") and not p.handles("Royal Caribbean")
    assert p.cruise_line == "Norwegian"


@pytest.mark.parametrize("ship,code", [
    ("Norwegian Getaway", "GETAWAY"), ("getaway", "GETAWAY"), ("NCL Prima", "PRIMA"),
    ("Pride of America", "PRIDE_AMER"), ("PRIDE_AMER", "PRIDE_AMER"), ("Norwegian  Aqua", "AQUA"),
    ("Carnival Magic", None), ("", None),
])
def test_ship_code_for(ship, code):
    assert ship_code_for(ship) == code


def test_parse_booking_url():
    url = ("https://www.ncl.com/vacation-builder/planning/stateroom?itineraryCode=GETAWAY3MIANASNPIMIA"
           "&packageId=24223987&guests=2&stateroomTypeCode=BALCONY")
    assert parse_booking_url(url) == (CODE, PKG)
    assert parse_booking_url("https://www.ncl.com/vacation-builder?itineraryCode=LUNA7MIAPOPSTTTOVNPIMIA") == (
        "LUNA7MIAPOPSTTTOVNPIMIA", None)
    assert parse_booking_url("https://www.carnival.com/x?itineraryCode=A") == (None, None)
    assert parse_booking_url("") == (None, None)


@pytest.mark.parametrize("text,embark,debark,expected", [
    ("7:00 AM - 5:00 PM", False, False, ("07:00", "17:00")),
    ("4:00 PM", True, False, (None, "16:00")),
    ("7:00 AM", False, True, ("07:00", None)),
    ("12:30 PM - 11:59 PM", False, False, ("12:30", "23:59")),
    ("9:00 AM", False, False, ("09:00", None)),  # first day of an overnight
    (None, False, False, (None, None)),
])
def test_parse_schedule(text, embark, debark, expected):
    assert parse_schedule(text, embark, debark) == expected


def test_ordered_ports():
    poc = [{"code": c, "title": c.title()} for c in ["HAL", "GIB", "MOT", "PDL", "FNC", "PMI", "SYN", "IBZ", "MLN"]]
    seq = "NYCATSEAHAL2SYN2ATSEA3PDL2ATSEAFNC2ATSEAGIB2MOT2MLN2IBZ2PMI2BCN"
    assert ordered_ports(seq, poc) == ["Hal", "Syn", "Pdl", "Fnc", "Gib", "Mot", "Mln", "Ibz", "Pmi"]
    # Codes with digits, and scenic stops that aren't ports of call.
    poc = [{"code": c, "title": c} for c in ["KAO", "HK1", "HSM", "KTN", "VIC"]]
    assert ordered_ports("HKGKAO2HK12KONHSM2GBKTN2VIC2SEA", poc) == ["KAO", "HK1", "HSM", "KTN", "VIC"]
    # A port missing from the sequence → the itinerary's own list.
    assert ordered_ports("SEAKTN2SEA", poc) == ["KAO", "HK1", "HSM", "KTN", "VIC"]


def test_map_days_sea_scenic_and_two_ports():
    events = [
        {"day": 1, "events": [{"eventType": "PORT", "date": "2027-01-10T16:00:00", "title": "Miami, Florida",
                               "portInfo": {"isEmbarkation": True, "dailySchedule": "4:00 PM"}}]},
        {"day": 2, "events": [{"eventType": "AT_SEA", "date": "2027-01-11T00:00:00", "title": "Eat. Drink. Play."}]},
        {"day": 3, "events": [{"eventType": "SCENIC", "title": "Daylight Transit Panama Canal"}]},
        {"day": 4, "events": [
            {"eventType": "PORT", "order": 1, "title": "Panama Canal (Gatun Lake), Panama",
             "portInfo": {"dailySchedule": "5:00 AM - 3:00 PM"}},
            {"eventType": "PORT", "order": 2, "title": "Colón, Panama", "portInfo": {"dailySchedule": "5:00 PM - 8:00 PM"}},
        ]},
    ]
    days = NorwegianProvider.map_days(events, "2027-01-10")
    assert [(d.day, d.date, d.port, d.arrive, d.depart) for d in days] == [
        (1, "2027-01-10", "Miami, Florida", None, "16:00"),
        (2, "2027-01-11", "At Sea", None, None),
        (3, "2027-01-12", "At Sea (Daylight Transit Panama Canal)", None, None),
        (4, "2027-01-13", "Panama Canal (Gatun Lake), Panama / Colón, Panama", "05:00", "20:00"),
    ]


# ── research ────────────────────────────────────────────────────────────


def test_research_room_types():
    site = NCLSite()
    r = provider(site).research(ResearchRequest("Norwegian", "Norwegian Getaway", "2027-05-07"))

    assert (r.cruise_line, r.ship, r.sail_date, r.nights) == ("Norwegian", "Norwegian Getaway", "2027-05-07", 3)
    assert r.departure_port == "Miami, Florida"
    assert r.itinerary_name.startswith("3-Day Bahamas")
    assert [(d.port, d.arrive, d.depart) for d in r.itinerary] == [
        ("Miami, Florida", None, "16:00"), ("Nassau, Bahamas", "07:00", "17:00"),
        ("Great Stirrup Cay, Bahamas", "07:00", "17:00"), ("Miami, Florida", "07:00", None)]

    rooms = {s.code: s for s in r.staterooms}
    # All-in category price minus the $200 pp taxes from price-summary.
    assert rooms["IX"].price_per_person == 159 and rooms["IX"].taxes_fees_per_person == 200
    assert rooms["IX"].category == "Interior" and rooms["IX"].name == "Guarantee Inside (IX)"
    assert "Guarantee" in rooms["IX"].notes and "Free at Sea adds $116.00 pp" in rooms["IX"].notes
    assert rooms["OB"].category == "Ocean View" and rooms["OB"].price_per_person == 209
    assert rooms["B1"].category == "Balcony" and rooms["B1"].price_per_person == 379
    assert rooms["M1"].category == "Suite" and rooms["M1"].price_per_person == 559
    assert rooms["HI"].category == "Suite" and rooms["HI"].price_per_person == 1479
    assert rooms["H2"].price_per_person == 3659
    # Solo studios / solo insides can't be booked for 2 guests.
    assert not {"T1", "IT", "OT", "BT"} & set(rooms)
    assert all(not s.sold_out for s in r.staterooms)
    # Cheapest first within each class, classes in price-sheet order.
    cats = [s.category for s in r.staterooms]
    assert cats == sorted(cats, key=["Interior", "Ocean View", "Balcony", "Suite"].index)
    assert r.staterooms[0].code == "IX"

    addons = {a.name: a for a in r.addons}
    assert addons["Free at Sea: Unlimited Open Bar"].price == 96 and addons["Free at Sea: Unlimited Open Bar"].kind == "beverage"
    assert addons["Free at Sea: Specialty Dining - 1 Meal"].price == 20
    assert addons["Free at Sea: Wi-Fi Package"].price == 0 and addons["Free at Sea: Wi-Fi Package"].kind == "internet"
    assert any("Free at Sea" in w for w in r.warnings)
    assert r.sources[0].endswith(f"itineraryCode={CODE}&packageId={PKG}&guests=2")

    # Found via the ship+month search; the price summary pre-selects the Free at Sea promotion.
    search = next(q for q in site.requests if q.url.path == "/api/v2/vacations/search")
    assert search.url.params["ships"] == "GETAWAY" and search.url.params["dates"] == "May-2027"
    summaries = [json.loads(q.content) for q in site.requests if q.url.path.endswith("/price-summary")]
    fares = {s["stateroomFilters"][0]["stateroomTypeCode"]: s["stateroomFilters"][0]["fareCodes"] for s in summaries}
    assert fares["INSIDE"] == ["ALL4CHO"] and fares["HAVEN"] == ["CHOALL4M"]
    # Never holds a cabin.
    assert not any("manage-cabin" in p for p in site.paths())


def test_research_from_booking_url_skips_search():
    site = NCLSite()
    url = f"https://www.ncl.com/vacation-builder/planning/stateroom?itineraryCode={CODE}&packageId={PKG}&guests=2"
    r = provider(site).research(ResearchRequest("Norwegian", "", "2027-05-07", booking_url=url))
    assert r.ship == "Norwegian Getaway" and r.sail_date == "2027-05-07"
    assert "/api/v2/vacations/search" not in site.paths()
    assert r.staterooms


def test_research_falls_back_to_room_type_lead_ins():
    site = NCLSite(fail_availability=True)
    r = provider(site).research(ResearchRequest("Norwegian", "Norwegian Getaway", "2027-05-07"))
    rooms = {s.code: s for s in r.staterooms}
    assert set(rooms) == {"INSIDE", "OCEANVIEW", "BALCONY", "MINISUITE", "HAVEN"}  # no solo studio
    assert rooms["INSIDE"].price_per_person == 179 and rooms["INSIDE"].taxes_fees_per_person == 200
    assert rooms["HAVEN"].category == "Suite" and rooms["HAVEN"].price_per_person == 1479
    assert any("lowest price per room type" in w for w in r.warnings)


def test_research_sold_out_lead_ins():
    rows = fx("sailings_GETAWAY3MIANASNPIMIA.json")["pricingStateRooms"]
    oct2 = NorwegianProvider.pick_sailing(rows, "2026-10-02")
    rooms = {s.code: s for s in NorwegianProvider.lead_in_rooms(oct2, None, "x")}
    assert rooms["OCEANVIEW"].sold_out and rooms["OCEANVIEW"].price_per_person is None
    assert rooms["INSIDE"].price_per_person == 339 and "all-in" in rooms["INSIDE"].notes


def test_research_unknown_ship_or_date():
    p = provider(NCLSite())
    with pytest.raises(ProviderError):
        p.research(ResearchRequest("Norwegian", "Carnival Magic", "2027-05-07"))
    with pytest.raises(ProviderError):
        p.research(ResearchRequest("Norwegian", "Norwegian Getaway", "2027-05-08"))


def test_research_wrong_ship_for_itinerary():
    url = f"https://www.ncl.com/vacation-builder?itineraryCode={CODE}&packageId={PKG}"
    with pytest.raises(ProviderError, match="not Norwegian Prima"):
        provider(NCLSite()).research(ResearchRequest("Norwegian", "Norwegian Prima", "2027-05-07", booking_url=url))


def test_blocked_without_cloudflare_raises():
    with pytest.raises(ProviderError):
        provider(NCLSite(status=403)).research(ResearchRequest("Norwegian", "Norwegian Getaway", "2027-05-07"))


class FakeCF:
    """Stands in for Cloudflare Browser Rendering: runs the in-page fetch against the fake site."""

    configured = True

    def __init__(self, site: NCLSite):
        self.site = site
        self.payloads: list[dict] = []

    def content(self, payload: dict) -> str:
        self.payloads.append(payload)
        script = payload["addScriptTag"][0]["content"]
        start = script.index("fetch(") + len("fetch(")
        path, opts = json.loads("[" + script[start:script.index(");out=")] + "]")
        req = httpx.Request(opts["method"], "https://www.ncl.com" + path, content=opts.get("body", "").encode())
        was, self.site.status = self.site.status, 200
        try:
            resp = self.site.handler(req)
        finally:
            self.site.status = was
        return f'<html><body><div id="qp-ncl" data-json="{quote(resp.text)}"></div></body></html>'


def test_blocked_switches_to_cloudflare_browser():
    site = NCLSite(status=403)
    cf = FakeCF(site)
    p = provider(site, cf=cf)
    r = p.research(ResearchRequest("Norwegian", "Norwegian Getaway", "2027-05-07"))
    assert {s.code: s.price_per_person for s in r.staterooms}["B1"] == 379
    assert cf.payloads and cf.payloads[0]["url"] == "https://www.ncl.com/vacations"
    # Only the first call went direct (403); every later one ran inside the browser page.
    assert len(cf.payloads) == len(site.requests) - 1


def test_retries_rate_limit():
    site = NCLSite()
    calls = {"n": 0}
    inner = site.handler

    def flaky(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"retry-after": "1"})
        return inner(req)

    p = NorwegianProvider(CloudflareBrowser("", ""), http=httpx.Client(transport=httpx.MockTransport(flaky)))
    slept = []
    p._sleep = slept.append
    assert p.search(ships="GETAWAY", dates="May-2027")[1]["code"] == CODE
    assert slept == [1.0, 1.0]


# ── catalog ─────────────────────────────────────────────────────────────


def test_iter_catalog():
    site = NCLSite(fail_summary_for=("24223989",))
    p = provider(site)
    rows = list(p.iter_catalog())
    assert [r.sailing_key for r in rows] == ["23468041", "24223987", "24223989"]
    oct2, may7, may21 = rows
    assert (may7.cruise_line, may7.ship, may7.ship_code, may7.sail_date, may7.nights) == (
        "Norwegian", "Norwegian Getaway", "GETAWAY", "2027-05-07", 3)
    assert may7.departure_port == "Miami, Florida"
    assert may7.ports == ["Nassau, Bahamas", "Great Stirrup Cay, Bahamas"]
    assert may7.taxes_fees_per_person == 200
    # All-in lead prices minus taxes; Suite = cheapest full suite (The Haven here); no solo Studio.
    assert may7.prices == {"Interior": 179, "Ocean View": 209, "Balcony": 259, "Club Balcony Suite": 309,
                           "The Haven": 1479, "Suite": 1479}
    assert oct2.prices["Ocean View"] is None and oct2.prices["Interior"] == 139 and oct2.prices["Suite"] is None
    # May 21's price summary failed: the nearest sailing of the same itinerary lends its taxes.
    assert may21.taxes_fees_per_person == 200 and may21.prices["The Haven"] == 1599
    assert may7.booking_url == f"https://www.ncl.com/vacation-builder/planning/stateroom?itineraryCode={CODE}&packageId={PKG}&guests=2"
    search = next(q for q in site.requests if q.url.path == "/api/v2/vacations/search")
    assert search.url.params["bundles"] == "cruise"
    assert p.calls == len(site.requests)


def test_iter_catalog_without_taxes_keeps_all_in_prices():
    rows = list(provider(NCLSite(), catalog_taxes=False).iter_catalog())
    may7 = next(r for r in rows if r.sailing_key == PKG)
    assert may7.taxes_fees_per_person is None and may7.prices["Interior"] == 379
