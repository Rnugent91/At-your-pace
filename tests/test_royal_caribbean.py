import json
from urllib.parse import quote

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.royal_caribbean import (
    RoyalCaribbeanProvider,
    parse_price,
    ship_code_for,
    unit_from_label,
)


def day(n, kind, port=None, arr=None, dep=None):
    ports = [] if port is None else [{"activity": None, "arrivalTime": arr, "departureTime": dep,
                                      "port": {"code": port[:3].upper(), "name": port, "region": None}}]
    return {"number": n, "type": kind, "ports": ports}


def cruise(cid, name, itype, total, aboard, sailings, ship=("UT", "Utopia of the Seas")):
    return {
        "id": cid,
        "sailings": [{"id": s, "sailDate": s.split("_")[1], "startDate": None, "endDate": None} for s in sailings],
        "masterSailing": {"itinerary": {
            "name": name, "code": "ITIN", "totalNights": total, "sailingNights": aboard, "type": itype,
            "ship": {"code": ship[0], "name": ship[1]},
            "departurePort": {"code": "PCV", "name": "Port Canaveral, Florida", "region": None},
            "destination": {"code": "BAH", "name": "Bahamas"},
            "days": [
                day(1, "EMBARK", "Port Canaveral, Florida", None, "16:30:00"),
                day(2, "PORT", "Nassau, Bahamas", "07:00:00", "17:00:00"),
                day(3, "AT_SEA"),
                day(4, "DEBARK", "Port Canaveral, Florida", "06:00:00", None),
            ],
        }},
    }


SEARCH = {"data": {"cruiseSearch": {"results": {"cruises": [
    # A CruiseTour on the same date must be skipped in favour of the real sailing.
    cruise("CT1", "3 Night Bahamas CruiseTour", "CRUISETOUR", 5, 3, ["UT03CT_2026-10-26"]),
    cruise("UT03PCV-123", "3 Night Bahamas & Perfect Day", "CRUISE", 3, 3,
           ["UT03BH01_2026-10-19", "UT03BH01_2026-10-26"]),
]}}}}

ROOM_HTML = """
<div data-testid="guarantee-content-container">
  <h3 data-testid="card-title">Balcony - We Choose Your Room</h3>
  <div data-testid="room-details-price"><span data-testid="main-price-amount">$600</span></div>
</div>
<div class="Room_content__abc">
  <h3 data-testid="card-title">Ocean View Balcony</h3>
  <div data-testid="room-details-price"><span data-testid="main-price-amount">$1,234.50</span></div>
</div>
<div class="Room_content__abc">
  <h3 data-testid="card-title">Infinite Balcony</h3>
</div>
"""


def gql_products(category):
    products = {
        "beverage": [{"id": "B1", "title": "Deluxe Beverage Package", "dayPorts": [],
                      "price": [{"formattedPromotionalPrice": "$79.99", "formattedBasePrice": "$99.99",
                                 "salesUnit": {"label": "per person, per day"}}],
                      "promotion": {"displayName": "20% off"}}],
        "internet": [{"id": "V1", "title": "VOOM Surf + Stream", "dayPorts": [],
                      "price": [{"formattedPromotionalPrice": None, "formattedBasePrice": "$24.99",
                                 "salesUnit": {"label": "per device, per day"}}], "promotion": None}],
        "shorex": [{"id": "S1", "title": "Beach Break", "dayPorts": [{"port": "Nassau, Bahamas"}],
                    "price": [{"formattedPromotionalPrice": "", "formattedBasePrice": "$79.00",
                               "salesUnit": {"label": "per person"}}], "promotion": None}],
    }.get(category, [])
    return {"data": {"products": {"commerceProducts": products, "pageInfo": {"totalPages": 1}}}}


class FakeCF(CloudflareBrowser):
    def __init__(self, room_html=ROOM_HTML):
        super().__init__("acct", "token")
        self.calls = []
        self.room_html = room_html

    def content(self, payload, attempts=3):
        self.calls.append(payload)
        if "cruises?search" in payload["url"]:
            return f"<html><body><div id='qp-sailings' data-json='{quote(json.dumps(SEARCH))}'></div></body></html>"
        return self.room_html if "cabinClassType=BALCONY" in payload["url"] else "<html></html>"


def graphql_transport(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json=gql_products(body["variables"]["category"]))


def provider(cf=None):
    http = httpx.Client(transport=httpx.MockTransport(graphql_transport))
    return RoyalCaribbeanProvider(cf or FakeCF(), "https://example.test/graphql", http=http)


def test_ship_codes():
    assert ship_code_for("Utopia of the Seas") == "UT"
    assert ship_code_for("utopia") == "UT"
    assert ship_code_for("  ic ") == "IC"
    assert ship_code_for("Carnival Jubilee") is None


def test_parse_price_and_units():
    assert parse_price("$1,234.50") == 1234.5
    assert parse_price(None) is None
    assert unit_from_label("per person, per day") == "per_person_per_day"
    assert unit_from_label("per device, per day") == "per_device_per_day"
    assert unit_from_label("per person") == "per_person"


def test_pick_sailing_skips_cruisetours():
    cruises = SEARCH["data"]["cruiseSearch"]["results"]["cruises"]
    c, s = RoyalCaribbeanProvider.pick_sailing(cruises, "2026-10-26")
    assert c["id"] == "UT03PCV-123" and s["id"] == "UT03BH01_2026-10-26"
    assert RoyalCaribbeanProvider.pick_sailing(cruises, "2026-11-01") is None


def test_parse_room_html_skips_guarantee_and_flags_sold_out():
    rooms = RoyalCaribbeanProvider.parse_room_html(ROOM_HTML, "Balcony", "src")
    assert [(r.name, r.price_per_person, r.sold_out) for r in rooms] == [
        ("Ocean View Balcony", 1234.5, False),
        ("Infinite Balcony", None, True),
    ]


def test_full_research():
    cf = FakeCF()
    r = provider(cf).research(ResearchRequest("Royal Caribbean", "Utopia of the Seas", "2026-10-26", adults=2, children=1))
    assert r.ship == "Utopia of the Seas" and r.nights == 3
    assert [d.port for d in r.itinerary] == ["Port Canaveral, Florida", "Nassau, Bahamas", "At Sea", "Port Canaveral, Florida"]
    assert r.itinerary[1].arrive == "07:00" and r.itinerary[2].date == "2026-10-28"
    assert [s.name for s in r.staterooms] == ["Ocean View Balcony", "Infinite Balcony"]
    # Room URL carries the real sailing's group/package codes and the party size.
    balcony_url = next(c["url"] for c in cf.calls if "cabinClassType=BALCONY" in c["url"])
    assert "groupId=UT03PCV-123" in balcony_url and "packageCode=UT03BH01" in balcony_url
    assert "r0a=2&r0c=1" in balcony_url
    drinks = next(a for a in r.addons if a.kind == "beverage")
    assert drinks.price == 79.99 and drinks.price_unit == "per_person_per_day" and "20% off" in drinks.description
    assert next(a for a in r.addons if a.kind == "excursion").port == "Nassau, Bahamas"
    assert any("Interior" in w for w in r.warnings)  # empty categories are flagged


def test_unknown_ship_and_missing_date_raise():
    with pytest.raises(ProviderError):
        provider().research(ResearchRequest("Royal Caribbean", "Nonexistent of the Seas", "2026-10-26"))
    with pytest.raises(ProviderError):
        provider().research(ResearchRequest("Royal Caribbean", "Utopia", "2027-01-01"))


def test_rooms_without_cloudflare_warn_but_keep_addons():
    p = provider(CloudflareBrowser("", ""))
    p.search_sailings = lambda code: SEARCH["data"]["cruiseSearch"]["results"]["cruises"]
    r = p.research(ResearchRequest("Royal Caribbean", "UT", "2026-10-26"))
    assert r.staterooms == [] and r.addons
    assert any("Cloudflare" in w for w in r.warnings)
