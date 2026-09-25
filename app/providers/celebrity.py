"""Celebrity Cruises: live prices from Celebrity's own systems.

Celebrity runs on the same Royal Caribbean Group platform as royalcaribbean.com,
so this reuses RoyalCaribbeanProvider with Celebrity's brand constants:

  * sailing lookup + itinerary + per-class fares
        celebritycruises.com/cruises/graph (cruiseSearch GraphQL, `brand: C` header).
        Unlike RC's page scraping, the search returns each stateroom class's
        lead-in fare with the taxes broken out, and it answers server IPs directly.
  * add-on prices
        aws-prd.api.rccl.com/en/celebrity/web/graphql (products GraphQL, Celebrity appkey).
  * per-room-type fares (optional)
        celebritycruises.com/room-selection pages. Akamai blocks these from server
        IPs, so they're only tried through Cloudflare Browser Rendering; when that
        isn't configured (or finds nothing) the per-class fares above are used.
"""

import logging
import re
from typing import Optional

import httpx

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import ProviderError, ResearchRequest
from .royal_caribbean import GRAPHQL_HEADERS, RoyalCaribbeanProvider, _hhmm

log = logging.getLogger(__name__)

BASE_URL = "https://www.celebritycruises.com"
SEARCH_URL = f"{BASE_URL}/cruises/graph"
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

# cruiseSearch stateroomClass.id → our category. Concierge Class and AquaClass
# are veranda staterooms with extra perks; The Retreat is Celebrity's suite class.
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

CRUISE_SEARCH_QUERY = """
query cruiseSearch_Cruises($filters: String, $sort: CruiseSearchSort, $pagination: CruiseSearchPagination) {
  cruiseSearch(filters: $filters, sort: $sort, pagination: $pagination) {
    results {
      cruises {
        id
        sailings {
          id sailDate
          taxesAndFees { value }
          taxesAndFeesIncluded
          stateroomClassPricing {
            price { value netAmount originalAmount taxesAndFeesAmount areTaxesAndFeesIncluded currency { code } }
            stateroomClass { id name }
          }
        }
        masterSailing { itinerary {
          name code totalNights sailingNights type
          ship { code name }
          departurePort { code name region }
          destination { code name }
          days { number type ports { activity arrivalTime departureTime port { code name region } } }
        } }
      }
    }
  }
}
"""


def ship_code_for(ship: str) -> Optional[str]:
    s = ship.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper() if s.upper() in _CODES else None
    key = re.sub(r"\s+", " ", s.lower())
    if not key.startswith("celebrity "):
        key = f"celebrity {key}"
    return SHIP_CODES.get(key)


def _port_name(port: dict) -> str:
    name = port.get("name") or "Port"
    region = port.get("region")
    return f"{name}, {region}" if region and region.lower() not in name.lower() else name


class CelebrityProvider(RoyalCaribbeanProvider):
    name = "Celebrity Cruises (live)"
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

        dep = itin.get("departurePort") or {}
        research = SailingResearch(
            cruise_line="Celebrity",
            ship=ship_name,
            sail_date=req.sail_date,
            nights=itin.get("sailingNights") or itin.get("totalNights"),
            departure_port=_port_name(dep) if dep else None,
            itinerary_name=itin.get("name"),
            itinerary=self.map_days(itin.get("days") or [], req.sail_date),
            currency="USD",
            staterooms=[],
            addons=[],
            sources=["celebritycruises.com cruise search"],
            warnings=[],
        )
        warnings = research.warnings  # pydantic copies lists passed in, so append to the model's own

        research.staterooms = self.class_fares(sailing, SEARCH_URL)
        if research.staterooms:
            if req.adults != 2 or req.children:
                warnings.append(
                    "Celebrity's per-class fares are lead-in prices for 2 adults sharing; "
                    "verify the fare for this party size."
                )
        else:
            warnings.append("Celebrity returned no stateroom prices for this sailing (sold out or not yet bookable).")

        package_code = sailing["id"].split("_")[0] if "_" in sailing.get("id", "") else itin.get("code", "")
        if self.cf.configured:
            try:
                detail = self.fetch_room_types(
                    code, req.sail_date, cruise.get("id", ""), package_code, req.adults, req.children
                )
            except Exception:  # keep the class fares if the room pages fail
                log.exception("Celebrity room-type fetch failed")
                detail = {}
            if detail:
                research.staterooms = self.merge_room_types(research.staterooms, detail)
                research.sources.append("celebritycruises.com room selection")

        research.addons = self.fetch_addons(code, req.sail_date, warnings)
        if research.addons:
            research.sources.append("Celebrity Cruise Planner")
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def search_sailings(self, ship_code: str, sail_date: str = "") -> list[dict]:
        filters = f"ship:{ship_code}" + (f"|startDate:{sail_date}~{sail_date}" if sail_date else "")
        payload = {
            "operationName": "cruiseSearch_Cruises",
            "variables": {
                "filters": filters,
                "sort": {"by": "RECOMMENDED", "order": "ASC"},
                "pagination": {"count": 100, "skip": 0},
            },
            "query": CRUISE_SEARCH_QUERY,
        }
        try:
            resp = self.http.post(SEARCH_URL, json=payload, headers=SEARCH_HEADERS)
            resp.raise_for_status()
            raw = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Celebrity cruise search failed ({exc})") from exc
        if raw.get("errors") and not raw.get("data"):
            raise ProviderError(f"Celebrity cruise search error: {str(raw['errors'])[:200]}")
        return (((raw.get("data") or {}).get("cruiseSearch") or {}).get("results") or {}).get("cruises") or []

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
                    port="At Sea" if at_sea else _port_name(port.get("port") or {}),
                    arrive=None if at_sea else _hhmm(port.get("arrivalTime")),
                    depart=None if at_sea else _hhmm(port.get("departureTime")),
                )
            )
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    @staticmethod
    def class_fares(sailing: dict, source: str) -> list[Stateroom]:
        """One row per stateroom class from cruiseSearch's lead-in pricing."""
        sailing_taxes = (sailing.get("taxesAndFees") or {}).get("value")
        rooms = []
        for entry in sailing.get("stateroomClassPricing") or []:
            cls = entry.get("stateroomClass") or {}
            cid = (cls.get("id") or "").upper()
            category = CABIN_CLASSES.get(cid, "Other")
            price = entry.get("price") or {}
            total = price.get("value")
            taxes = price.get("taxesAndFeesAmount", sailing_taxes)
            included = price.get("areTaxesAndFeesIncluded", sailing.get("taxesAndFeesIncluded"))
            fare = None
            if total is not None:
                fare = round(total - taxes, 2) if included and taxes is not None else float(total)
            notes = "Lead-in fare for the class (lowest available room type), per person, 2 guests sharing."
            if fare is not None and price.get("originalAmount") and price["originalAmount"] > total:
                notes += f" Was ${price['originalAmount']:,.2f} incl. taxes before current savings."
            rooms.append(
                Stateroom(
                    category=category,
                    name=CLASS_NAMES.get(cid) or cls.get("name") or cid.title(),
                    code=cid or None,
                    price_per_person=fare,
                    taxes_fees_per_person=taxes if fare is not None else None,
                    sold_out=total is None,
                    notes=notes if fare is not None else "Not available on Celebrity's site for this sailing.",
                    source=source,
                )
            )
        return rooms

    @staticmethod
    def room_url(ship_code: str, sail_date: str, group_id: str, package_code: str,
                 cabin_class: str, adults: int, children: int) -> str:
        return (
            f"{BASE_URL}/room-selection/room-subtype"
            f"?groupId={group_id}&packageCode={package_code}&sailDate={sail_date}"
            f"&country=USA&selectedCurrencyCode=USD&shipCode={ship_code}&cabinClassType={cabin_class}"
            f"&roomIndex=0&r0a={adults}&r0c={children}&r0b=n&r0r=n&r0s=n&r0q=n&r0t=n"
            f"&r0d={cabin_class}&r0D=y&rgVisited=true&r0C=y"
        )

    def fetch_room_types(self, ship_code, sail_date, group_id, package_code, adults, children) -> dict[str, list[Stateroom]]:
        """Per-room-type fares by class id, rendered through Cloudflare (Akamai blocks server IPs).

        Celebrity's room-selection app is the same one RC uses, so RC's HTML parser
        is reused. Classes where nothing parses are left out.
        """
        out: dict[str, list[Stateroom]] = {}
        for cabin_class, category in CABIN_CLASSES.items():
            url = self.room_url(ship_code, sail_date, group_id, package_code, cabin_class, adults, children)
            try:
                html = self.cf.content(
                    {
                        "url": url,
                        "gotoOptions": {"waitUntil": "networkidle0", "timeout": 30000},
                        "waitForSelector": {"selector": "[data-testid='card-title']", "timeout": 15000},
                    }
                )
            except BrowserRenderingError as exc:
                log.warning("Celebrity room render failed for %s: %s", cabin_class, exc)
                continue
            found = self.parse_room_html(html, category, url)
            if found:
                out[cabin_class] = found
        return out

    @staticmethod
    def merge_room_types(class_rows: list[Stateroom], detail: dict[str, list[Stateroom]]) -> list[Stateroom]:
        """Replace a class's lead-in row with its room types, keeping the class's tax figure."""
        merged: list[Stateroom] = []
        for row in class_rows:
            rooms = detail.get(row.code or "")
            if not rooms:
                merged.append(row)
                continue
            for r in rooms:
                r.taxes_fees_per_person = row.taxes_fees_per_person
                r.notes = f"{CLASS_NAMES.get(row.code or '', row.name)} room type; price as shown on Celebrity's room selection page."
                merged.append(r)
        return merged

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
