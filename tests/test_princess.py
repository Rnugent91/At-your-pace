"""Princess provider tests. Fixtures are trimmed captures of gw.api.princess.com responses
(Sept 2026); nothing here touches the network."""

import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from app.providers.base import ProviderError, ResearchRequest
from app.providers.princess import (
    PrincessProvider,
    clock24,
    guest_fare,
    ship_code_for,
    upgrade_price,
    voyage_from_url,
    ymd,
)

FIX = Path(__file__).parent / "fixtures" / "princess"
API = "https://gw.api.princess.com/pcl-web/internal"


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class PrincessApi:
    """A fake gw.api.princess.com backed by the fixtures; records what was asked for."""

    def __init__(self, status: int = 200, fail: tuple[str, ...] = ()):
        self.status = status
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def route(self, path: str, query: str, body: dict | None):
        if path == "/resdb/p1.0/ships":
            return fx("ships.json")
        if path == "/resdb/p1.0/ports":
            return fx("ports.json")
        if path == "/resdb/p1.0/metas":
            return fx("metas.json")
        if path == "/resdb/p1.0/products":
            assert "light=false" in query
            return fx("products.json")
        if path == "/resdb/p1.0/itineraries":
            assert query == "cruises=U644"
            return fx("itineraries_U644.json")
        if path == "/resdb/p1.0/ships/SU/3/categories":
            return fx("categories_SU_3.json")
        if path == "/ube/p1.0/ube":
            return {"ube": {"settings": {"features": {"premierPromos": ["RG3", "UN*"]}}}}
        if path == "/caps/pc/pricing/v1/cruises/U644":
            assert body["filters"]["cruiseType"] == "C" and body["retrieveFlags"]["subMeta"]
            return fx("pricing_U644.json")
        if path == "/caps/pc/pricing/v1/cruises/U644/specials":
            assert body["filters"]["promoFilters"] == ["RG3", "UN*"]
            return fx("specials_U644.json")
        if path.startswith("/db-excursion/p1.0/ports/"):
            assert query == "voyageId=U644"
            if path == "/db-excursion/p1.0/ports/CZM/excursions":
                return fx("excursions_CZM.json")
            if path == "/db-excursion/p1.0/ports/RTB/excursions":
                return None  # 404: reported as missing
            return {"excursions": []}
        if path == "/caps/pc/pricing/v1/cruises":
            assert body["leadInBy"] == "voyages" and body["filters"]["cruises"] == []
            return fx("lead_fares.json")
        return None

    PAGES = {
        "/cruise-dining/beverages": "page_beverages.html",
        "/html/global/disclaimers/crew-appreciation/": "page_crew.html",
        "/en-us/cruise-dining/crown-grill": "page_crown_grill.html",
    }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "www.princess.com":
            name = self.PAGES.get(request.url.path)
            if self.status != 200 or not name:
                return httpx.Response(self.status if self.status != 200 else 404, text="nope")
            return httpx.Response(200, text=(FIX / name).read_text(encoding="utf-8"))
        assert request.headers["pcl-client-id"] and request.headers["bookingcompany"] == "PC"
        path = request.url.path.replace("/pcl-web/internal", "")
        if self.status != 200:
            return httpx.Response(self.status, text="denied")
        if any(f in path for f in self.fail):
            return httpx.Response(500, json={"status": 500})
        body = json.loads(request.content) if request.content else None
        data = self.route(path, request.url.query.decode(), body)
        if data is None:
            return httpx.Response(404, json={"status": 404})
        return httpx.Response(200, json=data)

    def paths(self) -> list[str]:
        return [r.url.path.replace("/pcl-web/internal", "") for r in self.requests]


def provider(api: PrincessApi, cf=None) -> PrincessProvider:
    return PrincessProvider(cf=cf, http=httpx.Client(transport=httpx.MockTransport(api.handler)))


def req(**kw) -> ResearchRequest:
    base = dict(cruise_line="Princess", ship="Sun Princess", sail_date="2026-11-15")
    base.update(kw)
    return ResearchRequest(**base)


# ── helpers ──────────────────────────────────────────────────────────────


def test_helpers():
    assert ship_code_for("Sun Princess") == "SU"
    assert ship_code_for("sun") == "SU"
    assert ship_code_for("  Regal   Princess ") == "GP"
    assert ship_code_for("xp") == "XP"
    assert ship_code_for("Star Princess", {"star princess": "ST"}) == "ST"
    assert ship_code_for("Nieuw Statendam") is None
    assert voyage_from_url("https://www.princess.com/cruise-search/details/?voyageCode=U644A") == "U644A"
    assert voyage_from_url("https://book.princess.com/cruisepersonalizer/?voyage=4724&x=1") == "4724"
    assert voyage_from_url("https://www.princess.com/") is None
    assert ymd("20261115") == "2026-11-15" and ymd("") is None
    assert clock24("04:30 PM") == "16:30" and clock24("08:00 AM") == "08:00" and clock24("") is None


def test_guest_fare_and_upgrade():
    cat = {"guests": [{"id": 1, "fare": 399, "baseFare": 229}, {"id": 2, "fare": 399, "baseFare": 229},
                      {"id": 3, "fare": 170}]}
    assert guest_fare(cat, 2) == (229.0, 170.0)
    # 3rd guest only pays taxes/fees: average of 229, 229, 0 excl. taxes
    assert guest_fare(cat, 3) == (152.67, 170.0)
    assert guest_fare({"guests": []}) == (None, None)
    plus = {"IB": {"guests": [{"id": 1, "fare": 889, "baseFare": 719}]},
            "IA": {"guests": [{"id": 1, "fare": 1119, "baseFare": 949}]}}
    std = {"IB": cat, "IA": {"guests": [{"id": 1, "fare": 629, "baseFare": 459}]}}
    assert upgrade_price(std, plus, 2) == 490.0
    assert upgrade_price(std, {}, 2) is None


# ── research ─────────────────────────────────────────────────────────────


def test_research_by_ship_and_date():
    api = PrincessApi()
    r = provider(api).research(req())
    assert r.cruise_line == "Princess"
    assert r.ship == "Sun Princess" and r.sail_date == "2026-11-15" and r.nights == 7
    assert r.departure_port == "Ft. Lauderdale, Florida"
    assert r.itinerary_name == "Western Caribbean with Mexico"
    assert r.sources == ["https://www.princess.com/cruise-search/details/?voyageCode=U644"]
    # U644 (the ship's own 7-night voyage) beats the 14-night U644A combo starting the same day.
    assert any("U644A" in w for w in r.warnings)

    days = [(d.day, d.date, d.port, d.arrive, d.depart) for d in r.itinerary]
    assert days[0] == (1, "2026-11-15", "Ft. Lauderdale, Florida", None, "16:00")
    assert days[1][2] == "At Sea"
    assert days[2] == (3, "2026-11-17", "Cozumel, Mexico", "08:00", "18:00")
    assert days[-1] == (8, "2026-11-22", "Ft. Lauderdale, Florida", "06:00", None)
    assert len(days) == 8

    rooms = {s.code: s for s in r.staterooms if s.code}
    ib = rooms["IB"]
    assert (ib.category, ib.name) == ("Interior", "Interior (IB)")
    assert ib.price_per_person == 229.0 and ib.taxes_fees_per_person == 170.0
    assert "Princess Plus fare $719 pp" in ib.notes and "Princess Premier fare $964 pp" in ib.notes
    assert rooms["DF"].category == "Balcony" and rooms["DF"].name == "Deluxe Balcony (DF)"  # from ship categories
    assert rooms["C1"].category == "Suite" and "Mini-Suite" in rooms["C1"].name
    assert rooms["S0"].name == "Sky Suite (S0)" and "already includes" in rooms["S0"].notes
    # every priced category is its own row with a unique key
    assert len({s.key for s in r.staterooms}) == len(r.staterooms)
    sold = [s for s in r.staterooms if s.sold_out]
    assert sold and all(s.price_per_person is None for s in sold)
    cats = [s.category for s in r.staterooms]
    assert cats == sorted(cats, key=["Interior", "Ocean View", "Balcony", "Suite", "Other"].index)

    addons = {a.name: a for a in r.addons}
    assert addons["Princess Plus (fare upgrade)"].price == 490.0
    assert addons["Princess Premier (fare upgrade)"].price == 735.0
    assert all(a.price_unit == "per_person" for a in r.addons if "fare upgrade" in a.name)
    assert "per person per day" in addons["Princess Plus (fare upgrade)"].description


def test_research_addons():
    r = provider(PrincessApi()).research(req())
    addons = {a.name: a for a in r.addons}
    bev = [a for a in r.addons if a.kind == "beverage" and "fare upgrade" not in a.name]
    assert [a.name for a in bev] == ["Premier Beverage Package", "Plus Beverage Package", "Zero-Alcohol Package",
                                     "Classic Soda Package"]
    assert [a.price for a in bev] == [84.99, 64.99, 29.99, 14.99]
    assert all(a.price_unit == "per_person_per_day" and "20% service charge" in a.description for a in bev)
    grill = addons["Crown Grill"]  # Sun Princess: the Sun & Star price
    assert (grill.kind, grill.price, grill.price_unit) == ("dining", 60.0, "per_person")
    assert "$30.00" in grill.description
    crew = [a for a in r.addons if a.name.startswith("Crew appreciation")]
    assert [a.price for a in crew] == [20.0, 19.0, 18.0]
    assert all(a.price_unit == "per_person_per_day" for a in crew)
    ex = [a for a in r.addons if a.kind == "excursion"]
    assert [a.port for a in ex] == ["Cozumel, Mexico"] * 3
    assert ex[0].name.startswith("Jet Ski") and ex[0].price == 179.95 and ex[0].unit_label == "per adult"
    assert "includes meal" in ex[1].description
    assert any("Sabatini" in w and "Couldn't load" in w for w in r.warnings)
    assert any("Roatan" in w for w in r.warnings)
    assert any("published fleet-wide prices" in w for w in r.warnings)
    assert any("MedallionNet" in w for w in r.warnings)
    # Other ships get the fleet price
    from app.providers.princess import parse_specialty_dining
    text = (FIX / "page_crown_grill.html").read_text()
    assert parse_specialty_dining(text, "GP") == (55.0, 27.5)


def test_addon_failures_never_fail_research():
    r = provider(PrincessApi(fail=("/db-excursion",))).research(req())
    assert r.staterooms and not [a for a in r.addons if a.kind == "excursion"]
    assert any("shore excursions" in w for w in r.warnings)


def test_research_by_booking_url_and_guest_warning():
    api = PrincessApi()
    r = provider(api).research(req(ship="whatever", sail_date="2000-01-01", adults=3,
                                   booking_url="https://www.princess.com/cruise-search/details/?voyageCode=U644"))
    assert r.ship == "Sun Princess" and r.sail_date == "2026-11-15"
    assert any("average per person for 3 guests" in w for w in r.warnings)
    pricing_body = json.loads(next(q for q in api.requests if q.url.path.endswith("/cruises/U644")).content)
    assert len(pricing_body["booking"]["guests"]) == 3


def test_premier_failure_keeps_standard_and_plus():
    api = PrincessApi(fail=("/specials",))
    r = provider(api).research(req())
    assert r.staterooms
    assert [a.name for a in r.addons if "fare upgrade" in a.name] == ["Princess Plus (fare upgrade)"]


def test_unknown_ship_and_missing_sailing():
    with pytest.raises(ProviderError, match="Unknown Princess ship"):
        provider(PrincessApi()).research(req(ship="Queen Mary 2"))
    with pytest.raises(ProviderError, match="No Princess sailing"):
        provider(PrincessApi()).research(req(sail_date="2026-11-16"))


def test_handles():
    p = provider(PrincessApi())
    assert p.handles("Princess") and p.handles("princess cruises") and not p.handles("Holland America")


# ── catalog ──────────────────────────────────────────────────────────────


def test_iter_catalog():
    api = PrincessApi()
    rows = {r.sailing_key: r for r in provider(api).iter_catalog()}
    assert set(rows) == {"G639A", "U644A", "U644", "4724"}
    # one bulk pricing call covers every voyage
    assert api.paths().count("/caps/pc/pricing/v1/cruises") == 1
    alaska = rows["4724"]
    assert alaska.cruise_line == "Princess" and alaska.ship == "Star Princess" and alaska.ship_code == "ST"
    assert alaska.sail_date == "2027-06-13" and alaska.nights == 7
    assert alaska.departure_port == "Seattle, Washington"
    assert alaska.ports[0] == "Ketchikan, Alaska" and "Victoria, Canada" in alaska.ports
    assert alaska.booking_url.endswith("voyageCode=4724")
    assert set(alaska.prices) == {"Interior", "Ocean View", "Balcony", "Mini-Suite", "Suite"}
    assert all(v and v > 0 for v in alaska.prices.values())
    assert alaska.prices["Interior"] < alaska.prices["Suite"]
    assert alaska.taxes_fees_per_person and alaska.taxes_fees_per_person > 0
    # Sailing tomorrow: every class listed but sold out.
    assert rows["G639A"].prices and all(v is None for v in rows["G639A"].prices.values())


# ── Cloudflare fallback ──────────────────────────────────────────────────


class FakeCF:
    configured = True

    def __init__(self):
        self.payloads = []

    def content(self, payload: dict) -> str:
        self.payloads.append(payload)
        script = payload["addScriptTag"][0]["content"]
        data = {"ships": [{"id": "SU", "name": "Sun Princess"}]} if "/ships" in script else {}
        return f'<html><body><div id="qp-princess" data-json="{quote(json.dumps(data))}"></div></body></html>'


def test_blocked_requests_go_through_cloudflare():
    cf = FakeCF()
    p = provider(PrincessApi(status=403), cf=cf)
    assert p.ships() == {"SU": "Sun Princess"}
    payload = cf.payloads[0]
    assert payload["url"] == "https://www.princess.com/cruise-search/"
    assert f"{API}/resdb/p1.0/ships" in payload["addScriptTag"][0]["content"]
    assert "pcl-client-id" in payload["addScriptTag"][0]["content"]
    assert payload["waitForSelector"]["selector"] == "#qp-princess"
    # later calls skip the direct attempt
    p.ports()
    assert len(cf.payloads) == 2


def test_blocked_without_cloudflare_raises():
    p = provider(PrincessApi(status=403))
    with pytest.raises(ProviderError):
        p.products()
