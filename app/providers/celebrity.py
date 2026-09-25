"""Celebrity Cruises: live prices from Celebrity's own systems.

Celebrity runs on the same Royal Caribbean Group platform as royalcaribbean.com,
so this reuses RoyalCaribbeanProvider with Celebrity's brand constants:

  * sailing lookup, itinerary, per-class lead-in fares, master catalog
        celebritycruises.com/cruises/graph (cruiseSearch GraphQL, `brand: C` header),
        inherited from RoyalCaribbeanProvider (_search / iter_catalog).
  * individual room types (e.g. "Edge Stateroom with Infinite Veranda (E2)")
        celebritycruises.com/room-selection/api/v1/rooms — the JSON API behind the
        room-selection pages. One call with `options: true` returns every room
        type of every class with its price for the actual party size.
  * add-on prices
        aws-prd.api.rccl.com/en/celebrity/web/graphql (products GraphQL, Celebrity appkey).

All of these answer server IPs directly (Sept 2026). If Akamai starts blocking
them, the search and rooms calls are re-run from inside a celebritycruises.com
page rendered by Cloudflare Browser Rendering.
"""

import json
import logging
import re
import time
from typing import Optional
from urllib.parse import unquote, urlencode

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest
from .royal_caribbean import GRAPHQL_HEADERS, RoyalCaribbeanProvider, _hhmm

log = logging.getLogger(__name__)

BASE_URL = "https://www.celebritycruises.com"
SEARCH_URL = f"{BASE_URL}/cruises/graph"
ROOMS_API = f"{BASE_URL}/room-selection/api/v1/rooms"
GRAPHQL_URL = "https://aws-prd.api.rccl.com/en/celebrity/web/graphql"

# Celebrity's public web app key, as sent by celebritycruises.com itself.
APP_KEY = "qpRMO6lj4smwkT1sWlSdIj7b8QF5rG8Q"

# Ship name → Celebrity ship code (verified against cruiseSearch, Sept 2026).
# The lookup checks the code against the ship name Celebrity returns.
SHIP_CODES = {
    "celebrity xcel": "XC",
    "celebrity ascent": "AT",
    "celebrity beyond": "BY",
    "celebrity apex": "AX",
    "celebrity edge": "EG",
    "celebrity flora": "FL",
    "celebrity reflection": "RF",
    "celebrity silhouette": "SI",
    "celebrity eclipse": "EC",
    "celebrity equinox": "EQ",
    "celebrity solstice": "SL",
    "celebrity constellation": "CS",
    "celebrity summit": "SM",
    "celebrity infinity": "IN",
    "celebrity millennium": "ML",
    # Celebrity River Cruises (from 2027)
    "celebrity compass": "RC",
    "celebrity seeker": "RS",
    "celebrity wanderer": "RW",
    "celebrity roamer": "RR",
    "celebrity boundless": "RB",
}
_CODES = set(SHIP_CODES.values())

# Stateroom class id (cruiseSearch stateroomClass.id / rooms API stateroomType.code)
# → our category. Concierge Class and AquaClass are veranda staterooms with extra
# perks; The Retreat is Celebrity's suite class.
CABIN_CLASSES = {
    "INTERIOR": "Interior",
    "OUTSIDE": "Ocean View",
    "BALCONY": "Balcony",
    "CONCIERGE": "Balcony",
    "AQUA": "Balcony",
    "DELUXE": "Suite",
}
CLASS_NAMES = {
    "INTERIOR": "Inside",
    "OUTSIDE": "Ocean View",
    "BALCONY": "Veranda",
    "CONCIERGE": "Concierge Class",
    "AQUA": "AquaClass",
    "DELUXE": "The Retreat (Suites)",
}
# Master-catalog price keys: the shared classes plus Celebrity's own.
CATALOG_KEYS = {
    "INTERIOR": "Interior",
    "OUTSIDE": "Ocean View",
    "BALCONY": "Balcony",
    "CONCIERGE": "Concierge Class",
    "AQUA": "AquaClass",
    "DELUXE": "Suite",
}

# Celebrity's Cruise Planner category ids → our add-on kind. Bundles appear under
# both drinks and wifi; duplicates are dropped (first kind wins).
ADDON_CATEGORIES = [
    ("drinks", "beverage", False),
    ("wifi", "internet", False),
    ("food", "dining", True),
    ("shorex", "excursion", True),
    ("shipexcursions", "activity", True),
    ("photoPackage", "photo", True),
]

GRAPHQL_HEADERS_CEL = {
    **GRAPHQL_HEADERS,
    "appkey": APP_KEY,
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/",
    "req-app-id": "Celebrity.Web.PlanMyCruise",
}

SEARCH_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "brand": "C",
    "country": "USA",
    "currency": "USD",
    "language": "en",
    "office": "MIA",
    "countryalpha2code": "US",
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/cruises",
    "user-agent": USER_AGENT,
}

ROOMS_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "brand": "C",
    "country": "USA",
    "referer": f"{BASE_URL}/room-selection/rooms-and-guests",
    "user-agent": USER_AGENT,
}


def ship_code_for(ship: str) -> Optional[str]:
    s = ship.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper() if s.upper() in _CODES else None
    key = re.sub(r"\s+", " ", s.lower())
    if not key.startswith("celebrity "):
        key = f"celebrity {key}"
    return SHIP_CODES.get(key)


def _port_name(port: dict) -> Optional[str]:
    name = port.get("name")
    if not name:
        return None
    region = port.get("region")
    return f"{name}, {region}" if region and region.lower() not in name.lower() else name


def room_selection_url(group_id: str, package_code: str, sail_date: str, ship_code: str) -> str:
    """Deep link to the sailing's rooms-and-guests step on celebritycruises.com."""
    return (
        f"{BASE_URL}/room-selection/rooms-and-guests?groupId={group_id}&packageCode={package_code}"
        f"&sailDate={sail_date}&shipCode={ship_code}&country=USA&selectedCurrencyCode=USD"
    )


class CelebrityProvider(RoyalCaribbeanProvider):
    name = "Celebrity Cruises (live)"
    cruise_line = "Celebrity"
    site = BASE_URL
    search_url = SEARCH_URL
    search_headers = SEARCH_HEADERS
    cabin_classes = CABIN_CLASSES
    class_names = CLASS_NAMES
    graphql_headers = GRAPHQL_HEADERS_CEL
    addon_categories = ADDON_CATEGORIES
    addon_source = "Celebrity Cruise Planner"
    brand_label = "Celebrity"

    def __init__(self, cf: CloudflareBrowser, graphql_url: str = GRAPHQL_URL, http: Optional[httpx.Client] = None):
        super().__init__(cf, graphql_url, http)

    def handles(self, cruise_line: str) -> bool:
        return "celebrity" in cruise_line.lower()

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        code = ship_code_for(req.ship)
        if not code:
            raise ProviderError(f"Unknown Celebrity ship '{req.ship}' — enter its two-letter Celebrity ship code")

        cruises = self.search_sailings(code, req.sail_date)
        found = self.pick_sailing(cruises, req.sail_date)
        if not found:
            raise ProviderError(f"No Celebrity sailing found for ship {code} on {req.sail_date}")
        cruise, sailing = found
        itin = cruise["masterSailing"]["itinerary"]
        ship_name = (itin.get("ship") or {}).get("name") or req.ship
        if ship_code_for(ship_name) not in (code, None):
            raise ProviderError(f"Celebrity code {code} is {ship_name}, not {req.ship}")

        research = SailingResearch(
            cruise_line="Celebrity",
            ship=ship_name,
            sail_date=req.sail_date,
            nights=itin.get("sailingNights") or itin.get("totalNights"),
            departure_port=_port_name(itin.get("departurePort") or {}),
            itinerary_name=itin.get("name"),
            itinerary=self.map_days(itin.get("days") or [], req.sail_date),
            currency="USD",
            staterooms=[],
            addons=[],
            sources=["celebritycruises.com cruise search"],
            warnings=[],
        )
        warnings = research.warnings  # pydantic copies lists passed in, so append to the model's own

        package_code = sailing["id"].split("_")[0] if "_" in sailing.get("id", "") else itin.get("code", "")
        class_rows = self.search_class_fares(sailing)
        detail = self.room_types(code, req.sail_date, cruise.get("id", ""), package_code, req.adults, req.children)
        party_is_default = req.adults == 2 and not req.children
        if detail is None:
            detail = {}
            warnings.append("Individual room-type prices couldn't be loaded from Celebrity; showing class lead-in fares.")
        else:
            research.sources.append(room_selection_url(cruise.get("id", ""), package_code, req.sail_date, code))
            if not party_is_default:
                # The rooms API prices the real party; a class it leaves out has no room that fits it.
                guests = req.adults + req.children
                class_rows = [
                    r if r.code in detail else r.model_copy(update={
                        "price_per_person": None, "taxes_fees_per_person": None, "sold_out": True,
                        "notes": f"No {r.name} rooms available for {guests} guests on this sailing.",
                    })
                    for r in class_rows
                ]
        # Classes missing from both sources simply aren't sold on this ship (e.g. Flora is
        # all suites), so merge_rooms' per-class warnings are noise here.
        research.staterooms = self.merge_rooms(class_rows, detail, [])
        if not research.staterooms:
            warnings.append("Celebrity returned no stateroom prices for this sailing (sold out or not yet bookable).")
        if not party_is_default and any(r.code in CABIN_CLASSES and not r.sold_out for r in research.staterooms):
            warnings.append("Class lead-in fares from the cruise search are for 2 adults sharing; verify for this party size.")

        research.addons = self.fetch_addons(code, req.sail_date, warnings)
        if research.addons:
            research.sources.append("Celebrity Cruise Planner")
        return research

    # ── Sailing lookup / catalog (search and paging inherited) ───────────

    def _search_via_browser(self, ship_code: str, payload: dict) -> dict:
        text = self._fetch_in_page(
            "/cruises/graph", "POST", {k: v for k, v in SEARCH_HEADERS.items() if k not in ("user-agent", "origin", "referer")},
            json.dumps(payload), f"{BASE_URL}/cruises" + (f"?search=ship:{ship_code}" if ship_code else ""),
        )
        return json.loads(text)

    @staticmethod
    def _port_label(port: dict) -> Optional[str]:
        return _port_name(port)

    def catalog_rows(self, cruise: dict) -> list[CatalogSailing]:
        rows = super().catalog_rows(cruise)
        keys = {CLASS_NAMES[c]: CATALOG_KEYS[c] for c in CLASS_NAMES}
        for row in rows:
            row.prices = {keys.get(k, k): v for k, v in row.prices.items()}
            package_code = row.sailing_key.split("_")[0]
            row.booking_url = room_selection_url(cruise.get("id", ""), package_code, row.sail_date, row.ship_code or "")
        return rows

    @staticmethod
    def map_days(days: list[dict], sail_date: str) -> list[ItineraryDay]:
        from datetime import date, timedelta

        start = date.fromisoformat(sail_date)
        out = []
        for d in days:
            n = d.get("number", len(out) + 1)
            port = (d.get("ports") or [{}])[0] if d.get("ports") else {}
            at_sea = (d.get("type") or "").upper() in {"AT_SEA", "CRUISING"} or not port
            out.append(
                ItineraryDay(
                    day=n,
                    date=(start + timedelta(days=n - 1)).isoformat(),
                    port="At Sea" if at_sea else (_port_name(port.get("port") or {}) or "Port"),
                    arrive=None if at_sea else _hhmm(port.get("arrivalTime")),
                    depart=None if at_sea else _hhmm(port.get("departureTime")),
                )
            )
        return out

    # ── Individual room types ────────────────────────────────────────────

    def fetch_room_types(self, ship_code, sail_date, group_id, package_code, adults, children) -> dict[str, list[Stateroom]]:
        return self.room_types(ship_code, sail_date, group_id, package_code, adults, children) or {}

    def room_types(self, ship_code, sail_date, group_id, package_code, adults, children) -> Optional[dict[str, list[Stateroom]]]:
        """Every room type by class id from the room-selection API, or None if it failed.

        A single call returns all classes, so there's nothing to parallelize per sailing.
        """
        filt = {
            "countryCode": "USA",
            "packageId": package_code,
            "sailDate": sail_date,
            "currencyCode": "USD",
            "language": "en",
            "options": True,
            "roomNumbers": False,
            "rooms": [{"adultCount": adults, "childCount": children}],
        }
        query = urlencode({"filter": json.dumps(filt, separators=(",", ":"))})
        data = None
        try:
            data = self._rooms_direct(f"{ROOMS_API}?{query}")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Celebrity rooms API direct call failed: %s", exc)
            if self.cf.configured:
                try:
                    headers = {k: v for k, v in ROOMS_HEADERS.items() if k not in ("user-agent", "referer")}
                    data = json.loads(self._fetch_in_page(
                        f"/room-selection/api/v1/rooms?{query}", "GET", headers, None,
                        room_selection_url(group_id, package_code, sail_date, ship_code),
                    ))
                except (ProviderError, ValueError) as exc2:
                    log.warning("Celebrity rooms API via Cloudflare failed: %s", exc2)
        if not data or not (data.get("rooms") or [{}])[0].get("options"):
            return None
        source = room_selection_url(group_id, package_code, sail_date, ship_code)
        return self.parse_room_options(data, adults + children, source)

    def _rooms_direct(self, url: str) -> dict:
        for attempt in range(3):
            resp = self.http.get(url, headers=ROOMS_HEADERS)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise httpx.HTTPError("rooms API kept failing (rate limited or server error)")

    @staticmethod
    def parse_room_options(data: dict, guests: int, source: str) -> dict[str, list[Stateroom]]:
        guests = max(guests, 1)
        out: dict[str, list[Stateroom]] = {}
        types = (((data.get("rooms") or [{}])[0].get("options")) or {}).get("stateroomTypes") or []
        for t in types:
            cid = (t.get("code") or "").upper()
            category = CABIN_CLASSES.get(cid, "Other")
            rooms = []
            for s in t.get("stateroomSubtypes") or []:
                inv = (s.get("pricing") or {}).get("invoice") or {}
                taxes = inv.get("taxesAndFees")
                subtotal = inv.get("subtotal")
                if subtotal is None and inv.get("total") is not None:
                    subtotal = inv["total"] - (taxes or 0)
                cat = s.get("categoryCode") or s.get("code")
                guarantee = bool(s.get("guarantee"))
                notes = []
                if guarantee:
                    notes.append("Guarantee: Celebrity assigns the room in this category.")
                if s.get("roomsLeft"):
                    notes.append(f"{s['roomsLeft']} left at this price.")
                if guests != 2:
                    notes.append(f"Average per guest for {guests} guests sharing.")
                label = "Guarantee, " if guarantee else ""
                rooms.append(
                    Stateroom(
                        category=category,
                        name=f"{s.get('name') or cat} ({label}{cat})",
                        code=cat,
                        price_per_person=round(subtotal / guests, 2) if subtotal is not None else None,
                        taxes_fees_per_person=round(taxes / guests, 2) if taxes is not None else None,
                        sold_out=subtotal is None,
                        notes=" ".join(notes) or None,
                        source=source,
                    )
                )
            if rooms:
                out[cid] = rooms
        return out

    # ── Cloudflare fallback ──────────────────────────────────────────────

    def _fetch_in_page(self, path: str, method: str, headers: dict, body: Optional[str], page_url: str) -> str:
        """Run a same-origin fetch inside a Cloudflare-rendered celebritycruises.com page
        (so Akamai sees a real browser) and read the response back from the DOM."""
        if not self.cf.configured:
            raise ProviderError("Celebrity blocked a direct request and Cloudflare Browser Rendering isn't configured")
        opts = {"method": method, "headers": headers}
        if body is not None:
            opts["body"] = body
        script = (
            "(async function(){var out;try{out=await fetch(" + json.dumps(path) + "," + json.dumps(opts) + ")"
            ".then(function(r){return r.text();});}catch(e){out='ERR:'+(e&&e.message||e);}"
            "var d=document.createElement('div');d.id='qp-json';"
            "d.setAttribute('data-json',encodeURIComponent(out));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content(
                {
                    "url": page_url,
                    "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                    "addScriptTag": [{"content": script}],
                    "waitForSelector": {"selector": "#qp-json", "timeout": 55000},
                }
            )
        except BrowserRenderingError as exc:
            raise ProviderError(f"Celebrity request via Cloudflare failed: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="qp-json")
        if node is None:
            raise ProviderError("Celebrity page returned no data")
        text = unquote(node.get("data-json", ""))
        if text.startswith("ERR:"):
            raise ProviderError(f"Celebrity in-page request failed: {text[:200]}")
        return text

    # ── Add-ons ──────────────────────────────────────────────────────────

    def fetch_addons(self, ship_code: str, sail_date: str, warnings: list[str]) -> list[AddOn]:
        seen, out = set(), []
        for a in super().fetch_addons(ship_code, sail_date, warnings):
            k = (a.name.strip().lower(), a.port or "")
            if k in seen:
                continue
            seen.add(k)
            a.name = a.name.strip()
            out.append(a)
        return out
