"""Disney Cruise Line provider tests. Fixtures are trimmed live disneycruise.disney.go.com
responses (Sept 2026): the Disney Dream search for November 2026 (2 pages, 7 itineraries)
and sailing DD1529, 3-Night Very Merrytime Bahamian Cruise from Fort Lauderdale, 2026-11-20."""

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.disney import (DisneyProvider, booking_url, departure_from_name, party_mix,
                                  sailing_id_from_url, ship_code_for)

FIX = Path(__file__).parent / "fixtures" / "disney"


def load(name):
    return json.loads((FIX / name).read_text())


def sailings_file(body):
    return f"sailings_{body['productId']}_{body['itineraryId'].replace(',', '-')}.json"


def finder_fixture(path):
    """Fixture for a /dcl-cruise-101-webapi/finder/ path (Port Adventures, onboard activities)."""
    path = unquote(path)
    if path.endswith("/list-entity/onboard-activities/"):
        return load("onboard.json")
    if path.endswith("/list-entity/port-adventures/bahamas/"):
        return load("pa_bahamas.json")
    m = re.search(r"/details-entity/dcl/(\d+);entityType=port-of-call", path)
    if m and (FIX / f"port_{m.group(1)}.json").exists():
        return load(f"port_{m.group(1)}.json")
    return None


def make_transport(calls, fail=(), token_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        for f in fail:
            if f in path:
                return httpx.Response(500, text="boom")
        if path.endswith("/authz/private"):
            return httpx.Response(200, json={"successful": True})
        if path.endswith("/client-token/"):
            if token_status != 200:
                return httpx.Response(token_status, text="denied")
            return httpx.Response(200, json={"access_token": "tok123", "expires_in": 1800})
        if path.endswith("/get-client-token/"):
            return httpx.Response(200, json={"access_token": "c101tok", "expires_in": 1800})
        if "/dcl-cruise-101-webapi/finder/" in path:
            assert request.headers["x-access-token"] == "c101tok"
            data = finder_fixture(request.url.raw_path.decode())
            return httpx.Response(200, json=data) if data else httpx.Response(502, json={"errorCode": "X"})
        if path.endswith("/available-products/"):
            f = FIX / f"products_p{body['page']}.json"
            return httpx.Response(200, json=load(f.name)) if f.exists() else httpx.Response(200, json={"products": []})
        if path.endswith("/available-sailings/"):
            f = FIX / sailings_file(body)
            return httpx.Response(200, json=load(f.name) if f.exists() else {"sailings": []})
        if "/get-cruise-details-availability/" in path:
            assert request.headers["authorization"] == "BEARER tok123"
            if path.endswith("/DD1529"):
                return httpx.Response(200, json=load("details_DD1529.json"))
            return httpx.Response(404, json={"title": "Not Found"})
        if path.endswith("/stateroom-category-search"):
            assert request.headers["authorization"] == "BEARER tok123"
            if body["sailingId"] == "DD1529":
                return httpx.Response(200, json=load("categories_DD1529.json"))
            return httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def provider(calls, cf=None, **kw):
    p = DisneyProvider(cf or CloudflareBrowser("", ""), http=httpx.Client(transport=make_transport(calls, **kw)))
    p._sleep = lambda s: None
    return p


REQ = ResearchRequest(cruise_line="Disney Cruise Line", ship="Disney Dream", sail_date="2026-11-20")


def test_helpers():
    assert ship_code_for("Disney Dream") == "DD"
    assert ship_code_for("dream") == "DD"
    assert ship_code_for("Disney Wish") == "WW"
    assert ship_code_for("Treasure") == "WT"
    assert ship_code_for("Disney Destiny") == "WD"
    assert ship_code_for("Adventure") == "DA"
    assert ship_code_for("DM") == "DM"
    assert ship_code_for("Queen Mary 2") is None
    assert sailing_id_from_url(
        "https://disneycruise.disney.go.com/cruises-destinations/list/DD1529/3-Night-X/2026-11-20-Disney-Dream/") == "DD1529"
    assert sailing_id_from_url("https://disneycruise.disney.go.com/select-stateroom/WT0108/WT-VERANDAH") == "WT0108"
    assert sailing_id_from_url("https://disneycruise.disney.go.com/cruises-destinations/list/") is None
    assert departure_from_name("7-Night Western Caribbean Cruise from Port Canaveral") == "Port Canaveral"
    assert booking_url("DD1529", "3-Night Very Merrytime Bahamian Cruise from Fort Lauderdale", "2026-11-20",
                       "Disney Dream").endswith(
        "/list/DD1529/3-Night-Very-Merrytime-Bahamian-Cruise-from-Fort-Lauderdale/2026-11-20-Disney-Dream/")
    pm = party_mix(2, 1, sailing_avail=True)[0]
    assert pm["adultCount"] == 2 and pm["childCount"] == 1
    assert pm["nonAdultAges"] == [{"age": 8, "ageUnit": "YEAR"}] and pm["isDefault"] is True
    p = provider([])
    assert p.handles("Disney Cruise Line") and p.handles("Disney")
    assert not p.handles("Royal Caribbean")


def test_research_end_to_end():
    calls = []
    r = provider(calls).research(REQ)

    assert r.cruise_line == "Disney" and r.ship == "Disney Dream" and r.sail_date == "2026-11-20"
    assert r.nights == 3 and r.departure_port == "Fort Lauderdale, Florida"
    assert r.itinerary_name == "3-Night Very Merrytime Bahamian Cruise from Fort Lauderdale"
    assert r.sources[0].endswith("/list/DD1529/3-Night-Very-Merrytime-Bahamian-Cruise-from-Fort-Lauderdale/"
                                 "2026-11-20-Disney-Dream/")

    # The search is filtered to the ship and month, sorted by date.
    search = next(c for c in calls if c.url.path.endswith("/available-products/"))
    body = json.loads(search.content)
    assert body["filters"] == ["DD;filterType=ship", "2026-11;filterType=date"]
    assert body["sorts"][0]["criteria"] == "DATE" and body["partyMix"][0]["adultCount"] == 2

    days = r.itinerary
    assert [d.port for d in days] == ["Fort Lauderdale, Florida", "Disney Lookout Cay at Lighthouse Point",
                                      "Nassau, Bahamas", "Fort Lauderdale, Florida"]
    assert (days[0].arrive, days[0].depart) == (None, "16:00")
    assert (days[1].date, days[1].arrive, days[1].depart) == ("2026-11-21", "07:15", "17:00")
    assert (days[-1].arrive, days[-1].depart) == ("05:15", None)

    rooms = {s.code: s for s in r.staterooms}
    b = rooms["05B"]
    assert (b.category, b.name, b.price_per_person, b.taxes_fees_per_person) == (
        "Balcony", "Deluxe Oceanview Stateroom with Verandah", 1107.0, 137.0)
    assert rooms["04E"].name == "Deluxe Family Oceanview Stateroom with Extended Verandah"
    assert rooms["09D"].category == "Ocean View" and rooms["09D"].notes.endswith("1 stateroom available")
    assert rooms["01A"].category == "Suite" and rooms["01A"].price_per_person == 9501.0
    # Guarantees come from the cruise details (not in the category search).
    assert rooms["IGT"].price_per_person == 681.0 and rooms["IGT"].notes.startswith("Guarantee")
    assert rooms["VGT"].category == "Balcony"
    # 11C is sold out as a category but open as a guarantee: the guarantee replaces the sold-out row.
    assert rooms["11C"].name == "Standard Inside Stateroom (guarantee)" and rooms["11C"].price_per_person == 909.0
    assert sum(s.code == "11C" for s in r.staterooms) == 1
    assert rooms["06B"].sold_out and rooms["06B"].price_per_person is None
    # Grouped Interior → Suite, cheapest first within a class.
    cats = [s.category for s in r.staterooms]
    assert cats == sorted(cats, key=["Interior", "Ocean View", "Balcony", "Suite"].index)
    assert r.staterooms[0].code == "IGT"
    assert len(r.staterooms) == 29  # 26 categories + IGT/OGT/VGT + 11C guarantee - sold-out 11C

    add = {a.name: a for a in r.addons}
    assert add["Crew gratuities"].price == 16 and add["Crew gratuities"].price_unit == "per_person_per_day"
    assert "$48" in add["Crew gratuities"].description
    assert add["Crew gratuities (Concierge)"].price == 27.25

    # Port Adventures for the two ports of call (not Fort Lauderdale), from the Bahamas list.
    exc = [a for a in r.addons if a.kind == "excursion"]
    assert {a.port for a in exc} == {"Nassau, Bahamas", "Disney Lookout Cay at Lighthouse Point"}
    assert not any("castaway" in a.source for a in exc)  # Castaway Cay isn't on this sailing
    banana = add["Banana Boat (LPT08)"]
    assert banana.price == 59.0 and banana.price_unit == "per_person"
    assert banana.unit_label == "per person (ages 10 and up)" and "ages 8 to 9: $49.00" in banana.description
    assert banana.source.endswith("/port-adventures/lighthouse-point-banana-boat/")
    atv = add["Adventure by ATV - Per 2 person ATV (LPT24)"]
    assert atv.price == 289.0 and atv.price_unit == "flat" and atv.unit_label.startswith("per ATV")
    assert add["Adventure Jeeps, Beach & Local Lunch - Per Jeep (N10A)"].price_unit == "flat"
    # Dream's paid extras (Disney publishes no prices): Palo/Remy, spa, royal tea, tastings.
    assert {a.name for a in r.addons if a.kind == "dining"} == {"Palo", "Remy"}
    assert add["Palo"].price is None and "doesn't publish" in add["Palo"].description
    assert add["Senses Spa & Salon"].kind == "activity" and add["Royal Court Royal Tea"].kind == "activity"
    assert add["Beverage Tastings"].kind == "beverage"
    assert "Palo Steakhouse" not in add and "Enchanted Garden" not in add and "Cove Café" not in add
    assert any("published per-port prices" in w for w in r.warnings)
    assert any("Connect@Sea" in w for w in r.warnings)
    assert len(r.warnings) == 2

    # One client token per API family, one authz for the search.
    assert sum(c.url.path.endswith("/client-token/") for c in calls) == 1
    assert sum(c.url.path.endswith("/get-client-token/") for c in calls) == 1
    assert sum(c.url.path.endswith("/authz/private") for c in calls) == 1


def test_research_from_booking_url_skips_search():
    calls = []
    r = provider(calls).research(ResearchRequest(
        cruise_line="Disney", ship="", sail_date="",
        booking_url="https://disneycruise.disney.go.com/cruises-destinations/list/DD1529/x/2026-11-20-Disney-Dream/"))
    assert r.ship == "Disney Dream" and r.sail_date == "2026-11-20" and r.staterooms
    assert not any("available-products" in c.url.path for c in calls)


def test_party_mix_with_children():
    calls = []
    r = provider(calls).research(ResearchRequest(cruise_line="Disney", ship="Dream", sail_date="2026-11-20",
                                                 adults=2, children=1))
    cat_call = next(c for c in calls if c.url.path.endswith("/stateroom-category-search"))
    pm = json.loads(cat_call.content)["partyMix"][0]
    assert pm["childCount"] == 1 and pm["nonAdultAges"] == [{"age": 8, "ageUnit": "YEAR"}]
    assert any("age 8" in w for w in r.warnings)
    assert any("Party total" in (s.notes or "") for s in r.staterooms)


def test_category_search_down_falls_back_to_room_type_leads():
    r = provider([], fail=("stateroom-category-search",)).research(REQ)
    assert len(r.staterooms) == 14  # one per stateroom sub-type
    assert {s.code for s in r.staterooms} >= {"IGT", "11B", "07A", "04E", "01A"}
    assert any("lowest fare per room type" in w for w in r.warnings)
    assert r.itinerary


def test_details_down_falls_back_to_class_leadins():
    r = provider([], fail=("get-cruise-details-availability", "stateroom-category-search")).research(REQ)
    assert [s.category for s in r.staterooms] == ["Interior", "Ocean View", "Balcony", "Suite"]
    assert r.staterooms[0].price_per_person == 681.0 and r.staterooms[0].code == "IGT"
    assert r.itinerary == [] and r.nights == 3
    assert r.departure_port == "Fort Lauderdale"
    assert any("lowest fare per stateroom class" in w for w in r.warnings)


def test_unknown_ship_and_missing_sailing():
    with pytest.raises(ProviderError, match="Unknown Disney ship"):
        provider([]).research(ResearchRequest(cruise_line="Disney", ship="Nonexistent", sail_date="2026-11-20"))
    with pytest.raises(ProviderError, match="No Disney sailing"):
        provider([]).research(ResearchRequest(cruise_line="Disney", ship="Dream", sail_date="2026-11-21"))


def test_iter_catalog():
    calls = []
    rows = list(provider(calls).iter_catalog(today=date(2026, 9, 25), workers=2))
    assert len(rows) == 8 and len({r.sailing_key for r in rows}) == 8
    # 2 search pages + 7 itineraries (one available-sailings call each) + 1 authz
    assert sum(c.url.path.endswith("/available-products/") for c in calls) == 2
    assert sum(c.url.path.endswith("/available-sailings/") for c in calls) == 7
    row = next(r for r in rows if r.sailing_key == "DD1529")
    assert row.cruise_line == "Disney" and row.ship == "Disney Dream" and row.ship_code == "DD"
    assert row.sail_date == "2026-11-20" and row.nights == 3
    assert row.itinerary_name == "3-Night Very Merrytime Bahamian Cruise from Fort Lauderdale"
    assert row.departure_port == "Fort Lauderdale"
    assert row.ports == ["Disney Lookout Cay at Lighthouse Point", "Nassau, Bahamas"]
    assert list(row.prices) == ["Interior", "Ocean View", "Balcony", "Suite"]
    assert row.prices["Interior"] == 681.0 and row.taxes_fees_per_person == 137.0
    assert row.booking_url.endswith("/DD1529/3-Night-Very-Merrytime-Bahamian-Cruise-from-Fort-Lauderdale/"
                                    "2026-11-20-Disney-Dream/")
    # Past sailings are skipped.
    assert len(list(provider([]).iter_catalog(today=date(2026, 11, 21)))) == 3


class FakeCF:
    """Stands in for Cloudflare Browser Rendering: answers the in-page fetches from fixtures."""
    configured = True

    def __init__(self):
        self.payloads = []

    def content(self, payload):
        self.payloads.append(payload)
        script = payload["addScriptTag"][0]["content"]
        calls = json.loads(script[script.index("var calls=") + 10:script.index(";var strip=")])
        out = []
        for c in calls:
            if c["u"].endswith("/available-products/"):
                out.append(load(f"products_p{c['b']['page']}.json"))
            elif c["u"].endswith("/available-sailings/"):
                f = FIX / sailings_file(c["b"])
                out.append(load(f.name) if f.exists() else {"sailings": []})
            elif "/dcl-cruise-101-webapi/finder/" in c["u"]:
                assert c["a"] == "c101"
                out.append(finder_fixture(c["u"]))
            elif "get-cruise-details-availability" in c["u"]:
                assert c["a"] == "sa"
                out.append(load("details_DD1529.json"))
            elif c["u"].endswith("stateroom-category-search"):
                out.append(load("categories_DD1529.json"))
            else:
                out.append(None)
        return f'<html><body><div id="qp-dcl" data-json="{quote(json.dumps(out))}"></div></body></html>'


def test_blocked_direct_requests_switch_to_browser():
    calls = []
    cf = FakeCF()
    p = provider(calls, cf=cf, token_status=403, fail=())
    # Make the search refuse too.
    p.http = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(403, text="Access Denied"))[1]))
    r = p.research(REQ)
    assert p._use_browser
    assert r.ship == "Disney Dream" and any(s.code == "05B" for s in r.staterooms)
    assert all(urlparse(pl["url"]).netloc == "disneycruise.disney.go.com" for pl in cf.payloads)
    assert all(pl["waitForSelector"]["selector"] == "#qp-dcl" for pl in cf.payloads)
    # 2 search pages, available-sailings, details + categories, onboard + ports, region lists.
    assert len(cf.payloads) == 6
    assert any(a.kind == "excursion" and a.port == "Nassau, Bahamas" for a in r.addons)
    # Once switched, no more direct calls are attempted.
    n = len(calls)
    p.research(REQ)
    assert len(calls) == n


def test_blocked_without_cloudflare_raises():
    p = DisneyProvider(CloudflareBrowser("", ""),
                       http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="denied"))))
    with pytest.raises(ProviderError, match="refused"):
        p.research(REQ)


def test_rate_limit_is_retried():
    state = {"n": 0}
    inner = make_transport([])

    def handler(request):
        if request.url.path.endswith("/available-products/") and state["n"] < 2:
            state["n"] += 1
            return httpx.Response(429, headers={"retry-after": "1"})
        return inner.handle_request(request)

    p = DisneyProvider(CloudflareBrowser("", ""), http=httpx.Client(transport=httpx.MockTransport(handler)))
    slept = []
    p._sleep = slept.append
    r = p.research(REQ)
    assert r.staterooms and slept == [1.0, 1.0]


def test_addon_failures_never_fail_research():
    r = provider([], fail=("/dcl-cruise-101-webapi/",)).research(REQ)
    assert r.staterooms and r.itinerary
    assert [a.kind for a in r.addons] == ["other", "other"]  # gratuities still there
    assert any("onboard activities" in w for w in r.warnings)
    assert any("No Disney Port Adventures" in w for w in r.warnings)
