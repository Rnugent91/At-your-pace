"""Viking provider tests. Fixtures are trimmed captures of vikingcruises.com /
vikingrivercruises.com responses (Sept 2026); nothing here touches the network."""

import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from app.cf_browser import CloudflareBrowser
from app.providers.base import ProviderError, ResearchRequest
from app.providers.viking import (
    SITES,
    VikingProvider,
    category_for,
    departures_cover,
    nights_between,
    ship_key,
    sites_for,
    slug_from_url,
    split_suite_name,
)

FIX = Path(__file__).parent / "fixtures" / "viking"
OCEAN = "https://www.vikingcruises.com/oceans"
ITIN = "/oceans/cruise-destinations/western-mediterranean/iconic-western-mediterranean/index.html"
NO_CRUISE = {"cruiseId": "x", "cruises": None, "errors": ["Error retrieving pricing data."]}


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class VikingSite:
    """A fake Viking web server backed by the fixtures; records what was asked for."""

    def __init__(self, status: int = 200):
        self.status = status
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def post_json(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        if path.endswith("/Core/DnPCruiseFullInfo"):
            if body["cruiseId"] == "iconic-western-mediterranean":
                return json.loads(fx("ocean_cruise_full_info.json"))
            return {**NO_CRUISE, "cruiseId": body["cruiseId"]}
        if path.endswith("/Core/DnPSailingDetails"):
            assert body["sailingKey"] in {"20261016|OSE261016|OSE", "20261014|OMI261014|OMI"}
            return json.loads(fx("ocean_sailing_details.json"))
        raise AssertionError(path)

    def page(self, url: str):
        self.gets.append(url)
        if url == f"{OCEAN}/search-cruises/index.html":
            return fx("ocean_search.html")
        if url == f"{OCEAN}/ships/viking-sea.html":
            return f'<a href="{ITIN}">Iconic Western Mediterranean</a>'
        if url.startswith("https://www.vikingcruises.com" + ITIN):
            if "startLocation=rome" in url and "year=2026" in url:
                return fx("ocean_itinerary_rom_bcn_2026.html")
            return fx("ocean_itinerary_bcn_rom_2027.html")
        if url.endswith("/my-trip/silver-spirits-beverage-package/index.html"):
            return fx("silver_spirits.html")
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, text="Forbidden")
        url = str(request.url)
        if request.method == "POST":
            body = json.loads(request.content)
            assert not any(k.startswith("__") for k in body)  # browser-only hints never go to Viking
            assert request.url.params.get("v") == "11"
            return httpx.Response(200, json=self.post_json(request.url.path, body))
        html = self.page(url)
        return httpx.Response(200, text=html) if html is not None else httpx.Response(404, text="Not found")


def provider(site: VikingSite, cf: CloudflareBrowser = None) -> VikingProvider:
    http = httpx.Client(transport=httpx.MockTransport(site))
    return VikingProvider(cf or CloudflareBrowser("", ""), http=http)


# ── Helpers ─────────────────────────────────────────────────────────────


def test_ship_and_site_helpers():
    assert ship_key("Viking Mira") == ship_key("mira") == ship_key("  viking  MIRA ") == "mira"
    assert sites_for("Viking Octantis")[0].key == "expedition"
    assert sites_for("Mira")[0].key == "ocean"
    assert sites_for("Viking Eir")[0].key == "river"
    assert [s.key for s in sites_for("Viking Mira", "https://www.vikingrivercruises.com/x")] == ["river"]
    assert slug_from_url(
        "https://www.vikingcruises.com/oceans/cruise-destinations/western-mediterranean/iconic-western-mediterranean/pricing.html"
    ) == "iconic-western-mediterranean"
    assert slug_from_url(
        "https://www.vikingrivercruises.com/cruise-destinations/europe/rhine-getaway/2027-basel-amsterdam/index.html"
    ) == "rhine-getaway"
    assert slug_from_url("https://www.royalcaribbean.com/cruises") is None


def test_stateroom_categories():
    assert split_suite_name("Deluxe Veranda (DV3)") == ("Deluxe Veranda", "DV3")
    assert split_suite_name("Owner’s Suite (OS)") == ("Owner’s Suite", "OS")
    assert category_for("Veranda")[0] == "Balcony"
    assert category_for("Penthouse Veranda")[0] == "Balcony"
    assert category_for("Penthouse Junior Suite")[0] == "Suite"
    assert category_for("Nordic Balcony") == ("Balcony", category_for("Deluxe Nordic Balcony")[1])
    assert category_for("French Balcony")[0] == "Balcony" and "no step-out" in category_for("French Balcony")[1]
    assert category_for("Standard")[0] == "Ocean View"
    assert category_for("Mystery Cabin")[0] == "Other"


def test_dates():
    from datetime import date

    assert departures_cover("Sep 2026 - Aug 2029", date(2026, 10, 16))
    assert not departures_cover("Jan 2028 - Dec 2028", date(2026, 10, 16))
    assert departures_cover("", date(2026, 10, 16))
    assert nights_between("2026-10-16", "Oct 16 - Oct 23, 2026") == 7
    assert nights_between("2026-12-28", "Dec 28 - Jan 7, 2027") == 10
    assert nights_between("2026-12-28", "") is None


def test_parse_days_ranges_and_same_day_stops():
    exp = VikingProvider.parse_days(fx("expedition_itinerary.html"), "2026-12-15")
    assert [d.day for d in exp] == list(range(1, 14))
    assert exp[3].port == exp[9].port == "Explore Antarctica" and exp[12].date == "2026-12-27"

    river = VikingProvider.parse_days(fx("river_itinerary.html"), "2027-04-01")
    assert len(river) == 8
    assert river[3].port == "Speyer, Germany / Rüdesheim, Germany"
    assert river[4].port == "Scenic Sailing: Middle Rhine / Koblenz, Germany"


def test_variant_url_switches_direction_and_year():
    river = fx("river_itinerary.html")
    assert (VikingProvider.variant_url(river, "Amsterdam to Basel", "2026")
            == "/cruise-destinations/europe/rhine-getaway/2026-amsterdam-basel/index.html")
    ocean = fx("ocean_itinerary_bcn_rom_2027.html")
    assert VikingProvider.variant_url(ocean, "Rome (Civitavecchia) to Barcelona", "2026") == (
        f"{ITIN}?startLocation=rome&endLocation=barcelona&year=2026"
    )


# ── End to end ──────────────────────────────────────────────────────────


def test_full_research_by_ship_and_date():
    site = VikingSite()
    r = provider(site).research(ResearchRequest("Viking", "Viking Sea", "2026-10-16"))

    assert r.cruise_line == "Viking" and r.ship == "Viking Sea" and r.currency == "USD"
    assert r.itinerary_name == "Iconic Western Mediterranean" and r.nights == 7
    assert r.departure_port == "Rome (Civitavecchia)"
    # The ship page's itinerary is scanned first, with the site's promo code.
    first = site.posts[0]
    assert first[1]["cruiseId"] == "iconic-western-mediterranean" and first[1]["offerCode"] == "EBS"
    # Itinerary comes from the Rome→Barcelona 2026 page, not the default page.
    assert [d.port for d in r.itinerary][:2] == ["Rome (Civitavecchia), Italy", "Florence/Pisa (Livorno), Italy"]
    assert r.itinerary[-1].port == "Barcelona, Spain" and r.itinerary[-1].date == "2026-10-23"

    rooms = {s.code: s for s in r.staterooms}
    assert set(rooms) == {"OS", "ES2", "PV1", "DV3", "V1"}
    assert rooms["DV3"].category == "Balcony" and rooms["DV3"].name == "Deluxe Veranda (DV3)"
    assert rooms["DV3"].price_per_person == 4349 and rooms["DV3"].taxes_fees_per_person == 0.0
    assert "Standard fare $5,699" in rooms["DV3"].notes and "1 Left" in rooms["DV3"].notes
    assert rooms["OS"].category == "Suite" and rooms["OS"].price_per_person == 13999
    assert rooms["ES2"].sold_out and rooms["ES2"].price_per_person is None
    assert "Sold out (last fare $8,899)" in rooms["ES2"].notes
    assert len({s.key for s in r.staterooms}) == len(r.staterooms)

    drinks = next(a for a in r.addons if a.kind == "beverage")
    assert drinks.price == 27.0 and drinks.price_unit == "per_person_per_day"
    air = next(a for a in r.addons if a.kind == "other")
    assert air.price == 1499 and air.price_unit == "per_person"
    assert r.sources[0].endswith("iconic-western-mediterranean/pricing.html")


def test_booking_url_goes_straight_to_the_itinerary():
    site = VikingSite()
    url = "https://www.vikingcruises.com" + ITIN.replace("index.html", "pricing.html") + "?foo=1"
    r = provider(site).research(ResearchRequest("Viking", "Mira", "2026-10-14", booking_url=url))
    assert r.ship == "Viking Mira"
    assert f"{OCEAN}/search-cruises/index.html" not in site.gets  # no scan needed
    assert r.itinerary[0].port == "Barcelona, Spain"


def test_unknown_sailing_raises():
    with pytest.raises(ProviderError):
        provider(VikingSite()).research(ResearchRequest("Viking", "Viking Mars", "2026-10-16"))
    with pytest.raises(ProviderError):
        provider(VikingSite()).research(ResearchRequest("Viking", "", "2026-10-16"))


def test_blocked_without_cloudflare_raises():
    with pytest.raises(ProviderError) as err:
        provider(VikingSite(status=403)).research(
            ResearchRequest("Viking", "Viking Sea", "2026-10-16", booking_url="https://www.vikingcruises.com" + ITIN)
        )
    assert "CLOUDFLARE" in str(err.value)


class FakeCF(CloudflareBrowser):
    """Renders pages from the fixtures and runs the in-page API script's calls against them."""

    def __init__(self, site: VikingSite):
        super().__init__("acct", "token")
        self.site = site
        self.calls = []

    def content(self, payload, attempts=3):
        self.calls.append(payload)
        scripts = payload.get("addScriptTag") or []
        if not scripts:
            html = self.site.page(payload["url"])
            return html if html is not None else "<html>Not found</html>"
        js = scripts[0]["content"]
        bodies = json.loads(re.search(r"var bodies=(\[.*?\]);var out", js).group(1))
        path = re.search(r"fetch\('([^'?]+)", js).group(1)
        out = []
        for b in bodies:
            data = self.site.post_json(path, {k: v for k, v in b.items() if not k.startswith("__")})
            if data.get("cruises") and b.get("__date"):  # mirror the in-page trim
                data["cruises"] = [c for c in data["cruises"] if c["DepartureDateString"] == b["__date"]]
            out.append(data if data.get("cruises") or data.get("sailingData") else None)
        return f"<html><body><div id='qp-viking' data-json='{quote(json.dumps(out))}'></div></body></html>"


def test_falls_back_to_cloudflare_when_blocked():
    backend = VikingSite()
    cf = FakeCF(backend)
    p = provider(VikingSite(status=403), cf=cf)
    r = p.research(ResearchRequest("Viking", "Viking Sea", "2026-10-16"))
    assert r.ship == "Viking Sea" and r.itinerary and len(r.staterooms) == 5
    api_calls = [c for c in cf.calls if c.get("addScriptTag")]
    assert api_calls and all(c["url"].startswith(SITES["ocean"].base) for c in api_calls)
    assert any(path.endswith("DnPSailingDetails") for path, _ in backend.posts)
