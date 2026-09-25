"""Virgin Voyages provider tests. Fixtures are trimmed captures of Virgin's
bookvoyage-bff / GraphQL responses (Sept 2026); nothing here touches the network."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.virgin import (
    VirginVoyagesProvider,
    parse_voyage_id,
    ports_of_call,
    ship_code_for,
    to_24h,
    voyage_from_url,
)

FIX = Path(__file__).parent / "fixtures" / "virgin"


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class VirginApi:
    """A fake Virgin API backed by the fixtures; records every request."""

    def __init__(self, status: int = 200, cabin_status: int = 200, throttle: int = 0):
        self.status = status
        self.cabin_status = cabin_status
        self.throttle = throttle  # answer this many requests with 429 first
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.endswith("/book/api/auth?tokenType=guest"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "bearer tok"
        if self.throttle:
            self.throttle -= 1
            return httpx.Response(429, headers={"retry-after": "0"})
        if self.status != 200:
            return httpx.Response(self.status, text="blocked")
        if url.endswith("/bookvoyage-bff/v2/voyages"):
            body = json.loads(request.content)
            rng = body["searchQualifier"]["sailingDateRange"][0]
            if rng["start"] >= "2029-01-01":
                return httpx.Response(204)
            return httpx.Response(200, json=fx("voyages_search.json"))
        if "/bookvoyage-bff/sailings?" in url:
            assert request.url.params["voyageId"] == "SC2803117NSJRP"
            return httpx.Response(200, json=fx("sailing_details.json"))
        if url.endswith("/graphql"):
            if self.cabin_status != 200:
                return httpx.Response(self.cabin_status, text="nope")
            value = json.loads(request.content)["variables"]["value"]
            assert value["cabins"][0]["travelParty"] == [{"ageCategory": "ADULT", "count": 2}]
            return httpx.Response(200, json=fx("cabin_categories.json"))
        raise AssertionError(url)

    def paths(self, suffix: str) -> int:
        return sum(1 for r in self.requests if str(r.url).split("?")[0].endswith(suffix))


def provider(api: VirginApi, cf=None) -> VirginVoyagesProvider:
    return VirginVoyagesProvider(cf, httpx.Client(transport=httpx.MockTransport(api)))


def req(**kw) -> ResearchRequest:
    return ResearchRequest(**{"cruise_line": "Virgin Voyages", "ship": "Scarlet Lady", "sail_date": "2028-03-11", **kw})


def test_helpers():
    assert ship_code_for("Scarlet Lady") == "SC"
    assert ship_code_for("the brilliant lady") == "BR"
    assert ship_code_for("Valiant") == "VL"
    assert ship_code_for("rs") == "RS"
    assert ship_code_for("Mardi Gras") is None
    assert to_24h("05:00 PM") == "17:00" and to_24h("08:00 AM") == "08:00" and to_24h(None) is None
    assert parse_voyage_id("SC2803117NSJRP") == ("SC", "2028-03-11")
    assert parse_voyage_id("nonsense") is None
    url = "https://www.virginvoyages.com/book/voyage-planner/choose-a-cabin?packageCode=7NSJRP&voyageId=SC2803117NSJRP&cabins=1"
    assert voyage_from_url(url) == "SC2803117NSJRP"
    assert voyage_from_url("https://www.carnival.com/?voyageId=SC2803117NSJRP") is None
    ports = [{"name": "A ", "day": 1}, {"name": "B", "day": 2}, {"name": "B", "day": 3}, {"name": "C", "day": 5}, {"name": "A", "day": 8}]
    assert ports_of_call(ports) == ["B", "C"]
    p = provider(VirginApi())
    assert p.handles("Virgin Voyages") and p.handles("virgin") and not p.handles("Viking")
    assert p.cruise_line == "Virgin Voyages"


def test_research_room_types_and_itinerary():
    api = VirginApi()
    r = provider(api).research(req())
    assert (r.cruise_line, r.ship, r.sail_date, r.nights) == ("Virgin Voyages", "Scarlet Lady", "2028-03-11", 7)
    assert r.departure_port == "San Juan, Puerto Rico"
    assert r.itinerary_name == "Southern Caribbean Cruise"
    assert r.currency == "USD"

    days = {d.day: d for d in r.itinerary}
    assert len(days) == 8
    assert (days[1].port, days[1].arrive, days[1].depart) == ("San Juan, Puerto Rico", None, "18:00")
    assert (days[3].port, days[3].arrive) == ("At Sea", None)
    assert (days[4].port, days[4].date) == ("Bridgetown, Barbados", "2028-03-14")  # full name from the search
    assert days[8].arrive == "06:30"

    rooms = {x.code: x for x in r.staterooms}
    # Totals for the cabin ÷ 2 sailors, matching the site's "from $903 per Sailor".
    assert (rooms["IZ"].category, rooms["IZ"].price_per_person) == ("Interior", 903.0)
    assert (rooms["IN"].category, rooms["IN"].price_per_person) == ("Interior", 1075.0)
    assert (rooms["VZ"].category, rooms["VZ"].price_per_person) == ("Ocean View", 1323.0)
    assert (rooms["TZ"].category, rooms["TZ"].price_per_person) == ("Balcony", 1463.0)
    assert (rooms["SS"].category, rooms["SS"].price_per_person) == ("Suite", 3710.0)
    assert (rooms["SG"].category, rooms["SG"].price_per_person) == ("Suite", 5285.0)
    assert rooms["SG"].name == "Gorgeous Suite (Mega RockStar Quarters)"
    assert all(x.taxes_fees_per_person == 224.0 for x in r.staterooms)  # 448 tax total ÷ 2
    assert "Essential fare: $1,120 pp" in rooms["IN"].notes and "Premium fare: $1,435 pp" in rooms["IN"].notes
    # Solo / Social cabins (unavailable for two) and code-less entries are left out.
    assert not {"I1", "I4", "V1", "TS"} & set(rooms)
    assert all(not x.sold_out for x in r.staterooms)
    assert any("Wi-Fi" in w and "gratuities" in w and "Bar Tab" in w for w in r.warnings)
    assert r.sources[0].startswith("https://www.virginvoyages.com/book/voyage-planner/choose-a-cabin?")
    assert api.paths("/auth") == 1  # the guest token is reused


def test_research_from_booking_url_overrides_ship_and_date():
    api = VirginApi()
    url = "https://www.virginvoyages.com/book/voyage-planner/choose-a-cabin?packageCode=7NSJRP&voyageId=SC2803117NSJRP"
    r = provider(api).research(req(ship="", sail_date="2028-03-01", booking_url=url))
    assert (r.ship, r.sail_date) == ("Scarlet Lady", "2028-03-11")
    assert any("departs 2028-03-11" in w for w in r.warnings)


def test_unknown_ship_and_missing_sailing():
    with pytest.raises(ProviderError, match="Unknown Virgin Voyages ship"):
        provider(VirginApi()).research(req(ship="Oasis of the Seas"))
    with pytest.raises(ProviderError, match="No Virgin Voyages sailing"):
        provider(VirginApi()).research(req(sail_date="2028-03-12"))


def test_cabin_failure_keeps_itinerary_with_warning():
    r = provider(VirginApi(cabin_status=500)).research(req())
    assert r.itinerary and not r.staterooms
    assert any("Stateroom prices could not be loaded" in w for w in r.warnings)


def test_backs_off_on_429():
    api = VirginApi(throttle=2)
    r = provider(api).research(req())
    assert r.staterooms
    assert api.paths("/v2/voyages") == 3


def test_blocked_without_browser_raises():
    with pytest.raises(ProviderError, match="request failed"):
        provider(VirginApi(status=403)).research(req())


class FakeBrowser(CloudflareBrowser):
    """Answers the in-page fetch from the fixtures, as if run inside virginvoyages.com."""

    def __init__(self):
        super().__init__("acct", "token")
        self.payloads: list[dict] = []

    def content(self, payload: dict, attempts: int = 3) -> str:
        self.payloads.append(payload)
        script = payload["addScriptTag"][0]["content"]
        if "/v2/voyages" in script:
            data = fx("voyages_search.json")
        elif "/sailings?" in script:
            data = fx("sailing_details.json")
        else:
            data = fx("cabin_categories.json")
        return f'<html><body><div id="vv-json" data-json="{quote(json.dumps(data))}"></div></body></html>'


def test_browser_fallback_when_blocked():
    cf = FakeBrowser()
    r = provider(VirginApi(status=403), cf).research(req())
    assert len(cf.payloads) == 3
    assert cf.payloads[0]["url"].startswith("https://www.virginvoyages.com/book/")
    assert {x.code for x in r.staterooms} >= {"IZ", "VZ", "TZ", "SS"}


def test_iter_catalog():
    api = VirginApi()
    rows = list(provider(api).iter_catalog(today=date(2026, 9, 25), workers=2))
    by_key = {r.sailing_key: r for r in rows}
    assert set(by_key) == {"SC2803117NSJRP", "RS2709127NBVA", "VL2609275NNYB"}
    assert [r.sail_date for r in rows] == sorted(r.sail_date for r in rows)
    sc = by_key["SC2803117NSJRP"]
    assert (sc.cruise_line, sc.ship, sc.ship_code, sc.nights) == ("Virgin Voyages", "Scarlet Lady", "SC", 7)
    assert sc.departure_port == "San Juan, Puerto Rico"
    assert sc.ports == [
        "Charlotte Amalie, St. Thomas",
        "Bridgetown, Barbados",
        "Fort-de-France, Martinique",
        "Roseau, Dominica",
        "Philipsburg, St. Maarten",
    ]
    # Every sailing gets the same fixture cabins: class lead-ins are the cheapest available code.
    assert sc.prices == {"Interior": 903.0, "Ocean View": 1323.0, "Balcony": 1463.0, "Suite": 3710.0}
    assert sc.taxes_fees_per_person == 224.0
    assert "voyageId=SC2803117NSJRP" in sc.booking_url
    assert by_key["VL2609275NNYB"].ship == "Valiant Lady"
    # 4 yearly searches (the last answers 204) + 1 cabin call per sailing + 1 token.
    assert api.paths("/v2/voyages") == 4 and api.paths("/graphql") == 3 and api.paths("/auth") == 1


def test_iter_catalog_falls_back_to_search_lead_in():
    rows = list(provider(VirginApi(cabin_status=500)).iter_catalog(today=date(2026, 9, 25)))
    sc = next(r for r in rows if r.sailing_key == "SC2803117NSJRP")
    assert sc.prices == {"Interior": 903.0}


def test_class_prices_sold_out_class():
    cats = [
        {"code": "INSIDER", "submetas": [{"code": "IN", "isAvailable": False, "lowestAvailablePrice": {"totalPrice": {"amount": None}}}]},
        {"code": "SEA TERRACE", "submetas": [{"code": "TZ", "isAvailable": True, "lowestAvailablePrice": {"totalPrice": {"amount": 3000}}}]},
    ]
    assert VirginVoyagesProvider.class_prices(cats) == {"Interior": None, "Balcony": 1500.0}
