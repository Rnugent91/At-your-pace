"""Holland America provider tests. Fixtures are trimmed captures of hollandamerica.com
responses (Sept 2026); nothing here touches the network."""

import json
from pathlib import Path
from urllib.parse import parse_qs, quote

import httpx
import pytest

from app.providers.base import ProviderError, ResearchRequest
from app.providers.holland_america import (
    HollandAmericaProvider,
    class_price,
    clock24,
    cruise_id_from_url,
    ship_code_for,
    split_label,
)

FIX = Path(__file__).parent / "fixtures" / "holland_america"
SITE = "https://www.hollandamerica.com"


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def search_docs() -> list[dict]:
    docs = fx("search_2027-01-10.json")["response"]["docs"]
    tour = {**docs[1], "cruiseId": "J711", "tourId": "1T", "duration": 12,
            "contentPath": "/find-a-cruise/1t/j711"}
    return docs + [tour]


class HalSite:
    """A fake hollandamerica.com backed by the fixtures; records what was asked for."""

    def __init__(self, status: int = 200, price_status: int = 200, page_size_docs: int = 0):
        self.status = status
        self.price_status = price_status
        self.page_size_docs = page_size_docs
        self.requests: list[httpx.Request] = []

    def search(self, params: dict) -> dict:
        fq = params.get("fq", [])
        docs = search_docs()
        if any(f.startswith("cruiseId:") for f in fq):
            cid = next(f for f in fq if f.startswith("cruiseId:")).split(":", 1)[1]
            docs = [d for d in docs if d["cruiseId"] == cid]
        elif any(f.startswith("shipId:") for f in fq):
            ship = next(f for f in fq if f.startswith("shipId:")).split(":", 1)[1]
            day = next(f for f in fq if f.startswith("departDate:[2"))
            docs = [d for d in docs if d["shipId"] == ship and d["departDate"] in day]
        start, rows = int(params["start"][0]), int(params["rows"][0])
        return {"response": {"numFound": len(docs), "start": start, "docs": docs[start:start + rows]}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="Access Denied")
        path = request.url.path
        if path == "/search/halcruisesearch":
            assert request.headers["brand"] == "hal"
            return httpx.Response(200, json=self.search(parse_qs(request.url.query.decode())))
        if path == "/api/v2/price/cruise/J711":
            assert request.headers["clientid"] == "WEB" and request.headers["currencycode"] == "USD"
            if self.price_status != 200:
                return httpx.Response(self.price_status, json={"errors": ["x"]})
            return httpx.Response(200, json=fx("price_J711.json"))
        if path == "/bin/carnival/hal/us/en/find-a-cruise/c7w07a/j711/itinerarylistview.v2.json":
            return httpx.Response(200, json=fx("itinerary_J711.json"))
        return httpx.Response(404, text="not found")


def provider(site: HalSite, cf=None) -> HollandAmericaProvider:
    return HollandAmericaProvider(cf=cf, http=httpx.Client(transport=httpx.MockTransport(site.handler)))


def req(**kw) -> ResearchRequest:
    base = dict(cruise_line="Holland America", ship="Nieuw Statendam", sail_date="2027-01-10")
    base.update(kw)
    return ResearchRequest(**base)


# ── helpers ──────────────────────────────────────────────────────────────


def test_helpers():
    assert ship_code_for("Nieuw Statendam") == "NS"
    assert ship_code_for("ms Zuiderdam") == "UU"
    assert ship_code_for("aa") == "AA"
    assert ship_code_for("Sun Princess") is None
    assert cruise_id_from_url(f"{SITE}/en/us/find-a-cruise/c7w07a/j711") == "J711"
    assert cruise_id_from_url(f"{SITE}/en/us/booking/choose-your-guests?cruiseId=J711A") == "J711A"
    assert cruise_id_from_url(f"{SITE}/en/us") is None
    assert split_label("Zuiderdam#@#UU") == "Zuiderdam"
    assert clock24("16:00") == "16:00" and clock24("4:00 PM") == "16:00" and clock24("") is None


def test_class_price():
    doc = {"price_USD_IN_RESTRICTED_d": 799.0, "taxExpenses_USD_RESTRICTED": 160.0,
           "price_USD_OV_RESTRICTED_d": 0.0, "price_USD_OV_BASEPRICE_d": 984.0, "taxExpenses_USD_BASEPRICE": 160.0,
           "price_USD_SS_RESTRICTED_d": 0.0, "price_USD_SS_BASEPRICE_d": 0.0,
           "price_USD_PH_RESTRICTED_d": -1.0, "price_USD_PH_BASEPRICE_d": -1.0, "price_USD_PH_anonymous_d": -1.0}
    assert class_price(doc, "IN") == (639.0, 160.0, True)
    assert class_price(doc, "OV") == (824.0, 160.0, True)  # no lowest fare left: flexible cruise-only fare
    assert class_price(doc, "SS") == (None, None, True)  # sold out
    assert class_price(doc, "PH") == (None, None, False)  # not on this ship


def test_inactive_fares_still_priced_and_n2_is_have_it_all():
    data = {"roomTypes": [{"name": "Inside", "id": "NS_IN", "categories": [{"name": "Inside", "id": "NS_IN_IN", "price": [
        {"tax": 45, "price": 1894, "basePrice": 1849, "fare": "QA1", "classification": "restricted",
         "promoCodes": ["QA_USD"], "active": False},
        {"tax": 45, "price": 2874, "basePrice": 2829, "fare": "N24", "classification": "flexible",
         "promoCodes": ["N2_USD"], "active": False}]}]}]}
    rooms, hia = HollandAmericaProvider.map_staterooms(data, "src")
    assert rooms[0].price_per_person == 1849.0 and not rooms[0].sold_out
    assert hia == 980.0


# ── research ─────────────────────────────────────────────────────────────


def test_research_by_ship_and_date():
    site = HalSite()
    r = provider(site).research(req())
    assert r.cruise_line == "Holland America"
    assert r.ship == "Nieuw Statendam" and r.sail_date == "2027-01-10" and r.nights == 7  # J711, not 14-day J711A
    assert r.departure_port == "Fort Lauderdale, Florida, US"
    assert r.itinerary_name == "7-Day Western Caribbean: Greater Antilles & Mexico"
    assert r.sources == [f"{SITE}/en/us/find-a-cruise/c7w07a/j711"]

    days = [(d.day, d.date, d.port, d.arrive, d.depart) for d in r.itinerary]
    assert days[0] == (1, "2027-01-10", "Fort Lauderdale, Florida, US", None, "16:00")
    assert days[1] == (2, "2027-01-11", "RelaxAway Half Moon Cay, Bahamas", "09:00", "17:00")
    assert days[2] == (3, "2027-01-12", "At Sea", None, None)
    assert days[-1] == (8, "2027-01-17", "Fort Lauderdale, Florida, US", "07:00", None)

    rooms = {s.code: s for s in r.staterooms}
    inside = rooms["IN"]
    assert (inside.category, inside.name) == ("Interior", "Inside (IN)")
    assert inside.price_per_person == 639.0 and inside.taxes_fees_per_person == 160.0
    assert "restricted" in inside.notes and "Have It All fare $1,094 pp" in inside.notes
    assert rooms["OVN"].category == "Balcony" and rooms["OVN"].name == "Obstructed Verandah (OVN)"
    assert rooms["NS"].category == "Suite"
    assert rooms["SI"].sold_out and rooms["SI"].price_per_person is None and rooms["SI"].notes == "Sold out"
    assert len({s.key for s in r.staterooms}) == len(r.staterooms)
    cats = [s.category for s in r.staterooms]
    assert cats == sorted(cats, key=["Interior", "Ocean View", "Balcony", "Suite", "Other"].index)

    (hia,) = r.addons
    assert hia.name == "Have It All package (fare upgrade)"
    assert hia.price == 455.0 and hia.price_unit == "per_person"
    assert "65.00 per person per day" in hia.description


def test_research_by_booking_url():
    site = HalSite()
    r = provider(site).research(req(ship="anything", sail_date="1999-01-01", adults=4,
                                    booking_url=f"{SITE}/en/us/find-a-cruise/c7w07a/j711"))
    assert r.sail_date == "2027-01-10" and r.nights == 7  # the cruise, not the J711 cruisetour
    assert any("4 guests" in w for w in r.warnings)


def test_room_price_failure_falls_back_to_search_leads():
    r = provider(HalSite(price_status=500)).research(req())
    assert any("stateroom prices failed" in w for w in r.warnings)
    rooms = {s.code: s for s in r.staterooms}
    assert rooms["IN"].name == "Inside (lowest fare)" and rooms["IN"].price_per_person == 639.0
    assert "PH" not in rooms  # not offered on this ship
    assert r.addons and r.addons[0].price == 455.0  # from the search doc's HIA vs lowest fare


def test_unknown_ship_and_missing_sailing():
    with pytest.raises(ProviderError, match="Unknown Holland America ship"):
        provider(HalSite()).research(req(ship="Sun Princess"))
    with pytest.raises(ProviderError, match="No Holland America sailing"):
        provider(HalSite()).research(req(sail_date="2027-01-11"))


def test_handles():
    p = provider(HalSite())
    assert p.handles("Holland America") and p.handles("Holland America Line") and not p.handles("Princess")


# ── catalog ──────────────────────────────────────────────────────────────


def test_iter_catalog_pages_and_skips_cruisetours():
    site = HalSite()
    rows = list(provider(site).iter_catalog(page_size=2))
    assert [r.sailing_key for r in rows] == ["J711A", "J711"]
    searches = [r for r in site.requests if r.url.path == "/search/halcruisesearch"]
    assert len(searches) == 2  # 3 docs, 2 per page
    j711 = rows[1]
    assert j711.cruise_line == "Holland America" and j711.ship == "Nieuw Statendam" and j711.ship_code == "NS"
    assert j711.sail_date == "2027-01-10" and j711.nights == 7
    assert j711.departure_port == "Fort Lauderdale, Florida, US"
    assert j711.booking_url == f"{SITE}/en/us/find-a-cruise/c7w07a/j711"
    assert set(j711.ports) == {"Cozumel, Mexico", "RelaxAway Half Moon Cay, Bahamas", "Georgetown, Cayman Islands",
                               "Ocho Rios, Jamaica"}
    assert j711.prices["Interior"] == 639.0
    assert set(j711.prices) >= {"Interior", "Ocean View", "Balcony", "Suite", "Neptune Suite"}
    assert j711.taxes_fees_per_person == 160.0


# ── Cloudflare fallback ──────────────────────────────────────────────────


class FakeCF:
    configured = True

    def __init__(self):
        self.payloads = []

    def content(self, payload: dict) -> str:
        self.payloads.append(payload)
        data = fx("search_2027-01-10.json")
        return f'<html><body><div id="qp-hal" data-json="{quote(json.dumps(data))}"></div></body></html>'


def test_blocked_requests_go_through_cloudflare():
    cf = FakeCF()
    p = provider(HalSite(status=403), cf=cf)
    resp = p.search(["cruiseId:J711"])
    assert resp["docs"][0]["cruiseId"] == "J711A"
    payload = cf.payloads[0]
    assert payload["url"] == f"{SITE}/en/us/find-a-cruise"
    script = payload["addScriptTag"][0]["content"]
    assert "/search/halcruisesearch?" in script and '"brand": "hal"' in script


def test_blocked_without_cloudflare_raises():
    with pytest.raises(ProviderError, match="HTTP 403"):
        provider(HalSite(status=403)).search([])
