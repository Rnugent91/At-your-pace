"""Royal Caribbean: live prices straight from RC's own systems.

Ported from Quackport (DuckPassport.Web):
  * sailing lookup   — SailingSyncService  (cruiseSearch GraphQL, /cruises/graph)
  * add-on prices    — RcPricingService    (products GraphQL: drinks, Wi-Fi, excursions, dining...)
  * stateroom prices — RcPricingService + RcSailingDataService.BuildRoomUrl
                       (room-selection page rendered through Cloudflare Browser Rendering)
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import ProviderError, ResearchRequest

log = logging.getLogger(__name__)

# RC's public web app key, as sent by royalcaribbean.com itself (also used by Quackport).
APP_KEY = "trL6t38bpvA5p65XlCrhFKzug8NNkqCD"

# Ship name → RC ship code. The lookup verifies the code against the ship name RC
# returns, so a wrong or missing entry fails loudly rather than quoting the wrong
# ship. Advisors can also type the two-letter code directly.
SHIP_CODES = {
    "utopia of the seas": "UT",
    "icon of the seas": "IC",
    "star of the seas": "ST",
    "wonder of the seas": "WN",
    "symphony of the seas": "SY",
    "harmony of the seas": "HM",
    "oasis of the seas": "OA",
    "allure of the seas": "AL",
    "quantum of the seas": "QN",
    "anthem of the seas": "AN",
    "ovation of the seas": "OV",
    "spectrum of the seas": "SP",
    "odyssey of the seas": "OY",
    "freedom of the seas": "FR",
    "liberty of the seas": "LB",
    "independence of the seas": "ID",
    "voyager of the seas": "VY",
    "explorer of the seas": "EX",
    "adventure of the seas": "AD",
    "navigator of the seas": "NV",
    "mariner of the seas": "MA",
    "radiance of the seas": "RD",
    "brilliance of the seas": "BR",
    "serenade of the seas": "SE",
    "jewel of the seas": "JW",
    "enchantment of the seas": "EN",
    "grandeur of the seas": "GR",
    "vision of the seas": "VI",
}

# RC cabinClassType → our category (Quackport TrackedCruiseRoom.DisplayName).
CABIN_CLASSES = {
    "INTERIOR": "Interior",
    "OUTSIDE": "Ocean View",
    "BALCONY": "Balcony",
    "DELUXE": "Suite",
}

CLASS_NAMES = {
    "INTERIOR": "Interior",
    "OUTSIDE": "Ocean View",
    "BALCONY": "Balcony",
    "DELUXE": "Suite",
}

SEARCH_URL = "https://www.royalcaribbean.com/cruises/graph"
SEARCH_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "appkey": APP_KEY,
    "country": "USA",
    "currency": "USD",
    "language": "en",
    "office": "MIA",
    "countryalpha2code": "US",
    "origin": "https://www.royalcaribbean.com",
    "referer": "https://www.royalcaribbean.com/cruises",
    "user-agent": USER_AGENT,
}

# products GraphQL categories → our add-on kind
ADDON_CATEGORIES = [
    ("beverage", "beverage", False),
    ("internet", "internet", False),
    ("key", "other", False),
    ("dining", "dining", True),
    ("shorex", "excursion", True),
    ("onboardactivities", "activity", True),
    ("photoPackage", "photo", True),
]

CRUISE_SEARCH_QUERY = """
query cruiseSearch_Cruises($filters: String, $sort: CruiseSearchSort, $pagination: CruiseSearchPagination) {
  cruiseSearch(filters: $filters, sort: $sort, pagination: $pagination) {
    results {
      cruises {
        id
        sailings {
          id sailDate startDate endDate
          taxesAndFees { value }
          taxesAndFeesIncluded
          stateroomClassPricing {
            price { value originalAmount taxesAndFeesAmount areTaxesAndFeesIncluded }
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

PRODUCT_QUERY = """
query WebProductsByCategory($category: String!, $passengerId: String, $shipCode: ShipCodeScalar!,
  $sailDate: LocalDateScalar!, $reservationId: String, $pageSize: Long, $currentPage: Long,
  $sorting: Sorting, $filter: FilterInput, $currencyCode: String!) {
  products(category: $category, guestTypes: [ADULT], passengerId: $passengerId, shipCode: $shipCode,
    sailDate: $sailDate, reservationId: $reservationId, pageSize: $pageSize, currentPage: $currentPage,
    sorting: $sorting, filter: $filter, currencyIso: $currencyCode) {
    ... on CommerceProductResultSuccess {
      commerceProducts {
        id
        title
        dayPorts { port }
        price { formattedPromotionalPrice formattedBasePrice salesUnit { label } }
        promotion { displayName }
      }
      pageInfo { totalPages }
    }
  }
}
"""

GRAPHQL_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://www.royalcaribbean.com",
    "referer": "https://www.royalcaribbean.com/",
    "user-agent": "Mozilla/5.0",
    "appkey": APP_KEY,
    "account-id": "d936e3c5-fcd4-475d-a446-5ba602da3635",
    "vds-id": "d936e3c5-fcd4-475d-a446-5ba602da3635",
    "channel": "web",
    "gqlrouter": "products",
    "req-app-id": "Royal.Web.PlanMyCruise",
    "req-app-vers": "1.83.4",
    "x-apollo-operation-name": "WebProductsByCategory",
    "x-apollo-operation-type": "query",
}


def ship_code_for(ship: str) -> Optional[str]:
    s = ship.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    key = re.sub(r"\s+", " ", s.lower())
    if key in SHIP_CODES:
        return SHIP_CODES[key]
    if not key.endswith("of the seas") and f"{key} of the seas" in SHIP_CODES:
        return SHIP_CODES[f"{key} of the seas"]
    return None


def parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", "").replace("$", ""))
    return float(m.group()) if m else None


def unit_from_label(label: Optional[str]) -> str:
    lab = (label or "").lower()
    per_day = "day" in lab or "night" in lab
    if "device" in lab:
        return "per_device_per_day" if per_day else "per_device"
    return "per_person_per_day" if per_day else "per_person"


def _hhmm(t: Optional[str]) -> Optional[str]:
    return t[:5] if t else None


class RoyalCaribbeanProvider:
    name = "Royal Caribbean (live)"
    # Brand constants for the shared RCCL products GraphQL (overridden by CelebrityProvider).
    graphql_headers = GRAPHQL_HEADERS
    addon_categories = ADDON_CATEGORIES
    addon_source = "Royal Caribbean Cruise Planner"
    brand_label = "Royal Caribbean"

    def __init__(self, cf: CloudflareBrowser, graphql_url: str, http: Optional[httpx.Client] = None):
        self.cf = cf
        self.graphql_url = graphql_url
        self.http = http or httpx.Client(timeout=30)

    def handles(self, cruise_line: str) -> bool:
        return "royal" in cruise_line.lower()

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        code = ship_code_for(req.ship)
        if not code:
            raise ProviderError(f"Unknown Royal Caribbean ship '{req.ship}' — enter its two-letter RC code")

        cruises = self.search_sailings(code, req.sail_date)
        found = self.pick_sailing(cruises, req.sail_date)
        if not found:
            raise ProviderError(f"No Royal Caribbean sailing found for ship {code} on {req.sail_date}")
        cruise, sailing = found
        itin = cruise["masterSailing"]["itinerary"]
        ship_name = (itin.get("ship") or {}).get("name") or req.ship
        if not ship_code_for(ship_name) in (code, None):
            raise ProviderError(f"RC code {code} is {ship_name}, not {req.ship}")

        research = SailingResearch(
            cruise_line="Royal Caribbean",
            ship=ship_name,
            sail_date=req.sail_date,
            nights=itin.get("sailingNights") or itin.get("totalNights"),
            departure_port=(itin.get("departurePort") or {}).get("name"),
            itinerary_name=itin.get("name"),
            itinerary=self.map_days(itin.get("days") or [], req.sail_date),
            currency="USD",
            staterooms=[],
            addons=[],
            sources=["royalcaribbean.com cruise search"],
            warnings=[],
        )
        warnings = research.warnings  # pydantic copies lists passed in, so append to the model's own

        package_code = sailing["id"].split("_")[0] if "_" in sailing.get("id", "") else itin.get("code", "")
        class_rows = self.search_class_fares(sailing)
        detail: dict[str, list[Stateroom]] = {}
        if self.cf.configured:
            try:
                detail = self.fetch_room_types(
                    code, req.sail_date, cruise.get("id", ""), package_code, req.adults, req.children
                )
            except Exception:  # keep the class fares, itinerary and add-ons even if rooms fail
                log.exception("RC room-type fetch failed")
            if detail:
                research.sources.append("royalcaribbean.com room selection")
        else:
            warnings.append(
                "Per-room-type prices need Cloudflare Browser Rendering (RC blocks server requests to its "
                "room pages). Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN."
            )
        research.staterooms = self.merge_rooms(class_rows, detail, warnings)
        if class_rows and (req.adults != 2 or req.children):
            warnings.append("Room-class fares from the cruise search are for 2 adults sharing; verify for this party size.")

        research.addons = self.fetch_addons(code, req.sail_date, warnings)
        if research.addons:
            research.sources.append("Royal Caribbean Cruise Planner")
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def search_sailings(self, ship_code: str, sail_date: str = "") -> list[dict]:
        filters = f"ship:{ship_code}" + (f"|startDate:{sail_date}~{sail_date}" if sail_date else "")
        payload = {
            "operationName": "cruiseSearch_Cruises",
            "variables": {
                "filters": filters,
                "sort": {"by": "RECOMMENDED", "order": "ASC"},
                "pagination": {"count": 200, "skip": 0},
            },
            "query": CRUISE_SEARCH_QUERY,
        }
        # RC's search answers server IPs directly (Sept 2026); fall back to a
        # Cloudflare-rendered page if Akamai starts blocking it.
        try:
            resp = self.http.post(SEARCH_URL, json=payload, headers=SEARCH_HEADERS)
            resp.raise_for_status()
            raw = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if not self.cf.configured:
                raise ProviderError(
                    f"Royal Caribbean search blocked or failed ({exc}). Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN."
                ) from exc
            log.warning("RC direct search failed (%s); using Cloudflare", exc)
            raw = self._search_via_browser(ship_code, payload)
        return (((raw.get("data") or {}).get("cruiseSearch") or {}).get("results") or {}).get("cruises") or []

    def _search_via_browser(self, ship_code: str, payload: dict) -> dict:
        # Load an RC page (sets Akamai cookies) and run the search from inside it,
        # stashing the JSON in a DOM element we can read back from the HTML.
        script = (
            "(async function(){var out;try{out=await fetch('/cruises/graph',{method:'POST',"
            "headers:{'content-type':'application/json','appkey':'" + APP_KEY + "'},"
            "body:" + json.dumps(json.dumps(payload)) + "}).then(function(r){return r.text();});}"
            "catch(e){out='ERR:'+(e&&e.message||e);}"
            "var d=document.createElement('div');d.id='qp-sailings';"
            "d.setAttribute('data-json',encodeURIComponent(out));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content(
                {
                    "url": f"https://www.royalcaribbean.com/cruises?search=ship:{ship_code}",
                    "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                    "addScriptTag": [{"content": script}],
                    "waitForSelector": {"selector": "#qp-sailings", "timeout": 55000},
                }
            )
        except BrowserRenderingError as exc:
            raise ProviderError(f"Royal Caribbean search failed: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="qp-sailings")
        if node is None:
            raise ProviderError("Royal Caribbean search returned no data")
        text = unquote(node.get("data-json", ""))
        if text.startswith("ERR:"):
            raise ProviderError(f"Royal Caribbean search failed in-page: {text[:200]}")
        return json.loads(text)

    @staticmethod
    def pick_sailing(cruises: list[dict], sail_date: str) -> Optional[tuple[dict, dict]]:
        """The pure-cruise itinerary sailing on `sail_date` (skips CruiseTours / hotel packages)."""
        for cruise in cruises:
            itin = (cruise.get("masterSailing") or {}).get("itinerary") or {}
            name = (itin.get("name") or "").replace(" ", "").lower()
            land = (
                (itin.get("type") or "").upper() == "CRUISETOUR"
                or "cruisetour" in name
                or (itin.get("totalNights") or 0) > (itin.get("sailingNights") or itin.get("totalNights") or 0)
            )
            if land:
                continue
            for sailing in cruise.get("sailings") or []:
                if (sailing.get("sailDate") or "")[:10] == sail_date:
                    return cruise, sailing
        return None

    @staticmethod
    def map_days(days: list[dict], sail_date: str) -> list[ItineraryDay]:
        from datetime import date, timedelta

        start = date.fromisoformat(sail_date)
        out = []
        for d in days:
            port = (d.get("ports") or [{}])[0] if d.get("ports") else {}
            at_sea = (d.get("type") or "").upper() in {"AT_SEA", "CRUISING"} or not port
            out.append(
                ItineraryDay(
                    day=d.get("number", len(out) + 1),
                    date=(start + timedelta(days=d.get("number", len(out) + 1) - 1)).isoformat(),
                    port="At Sea" if at_sea else ((port.get("port") or {}).get("name") or "Port"),
                    arrive=None if at_sea else _hhmm(port.get("arrivalTime")),
                    depart=None if at_sea else _hhmm(port.get("departureTime")),
                )
            )
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    @staticmethod
    def room_url(ship_code: str, sail_date: str, group_id: str, package_code: str,
                 cabin_class: str, adults: int, children: int) -> str:
        return (
            "https://www.royalcaribbean.com/room-selection/room-subtype"
            f"?groupId={group_id}&packageCode={package_code}&sailDate={sail_date}"
            f"&country=USA&selectedCurrencyCode=USD&shipCode={ship_code}&cabinClassType={cabin_class}"
            f"&roomIndex=0&r0a={adults}&r0c={children}&r0b=n&r0r=n&r0s=n&r0q=n&r0t=n"
            f"&r0d={cabin_class}&r0D=y&rgVisited=true&r0C=y"
        )

    def search_class_fares(self, sailing: dict) -> list[Stateroom]:
        """One lead-in row per room class from the cruise search's pricing."""
        sailing_taxes = (sailing.get("taxesAndFees") or {}).get("value")
        rows = []
        for entry in sailing.get("stateroomClassPricing") or []:
            cid = ((entry.get("stateroomClass") or {}).get("id") or "").upper()
            if cid not in CABIN_CLASSES:
                continue
            price = entry.get("price") or {}
            total = price.get("value")
            taxes = price.get("taxesAndFeesAmount", sailing_taxes)
            included = price.get("areTaxesAndFeesIncluded", sailing.get("taxesAndFeesIncluded"))
            fare = None
            if total is not None:
                fare = round(total - taxes, 2) if included and taxes is not None else float(total)
            notes = "Lead-in fare for the class (lowest available room type), per person, 2 guests sharing."
            if fare is not None and (price.get("originalAmount") or 0) > total:
                notes += f" Was ${price['originalAmount']:,.2f} incl. taxes before current savings."
            rows.append(
                Stateroom(
                    category=CABIN_CLASSES[cid],
                    name=CLASS_NAMES[cid],
                    code=cid,
                    price_per_person=fare,
                    taxes_fees_per_person=taxes if fare is not None else None,
                    sold_out=total is None,
                    notes=notes if fare is not None else "Not available on Royal Caribbean's site for this sailing.",
                    source=SEARCH_URL,
                )
            )
        return rows

    def fetch_room_types(self, ship_code, sail_date, group_id, package_code, adults, children) -> dict[str, list[Stateroom]]:
        """Per-room-type fares by cabin class, from room pages rendered through Cloudflare.

        The four classes load in parallel (Cloudflare's paid plan allows many
        concurrent browsers; on the free plan the client backs off on 429s).
        """

        def one(cabin_class: str) -> tuple[str, list[Stateroom]]:
            category = CABIN_CLASSES[cabin_class]
            url = self.room_url(ship_code, sail_date, group_id, package_code, cabin_class, adults, children)
            for try_url in (url, url.replace("/room-selection/room-subtype", "/room-selection/type-and-subtype")):
                try:
                    html = self.cf.content(
                        {
                            "url": try_url,
                            "gotoOptions": {"waitUntil": "networkidle0", "timeout": 30000},
                            "waitForSelector": {"selector": "[data-testid='card-title']", "timeout": 15000},
                        }
                    )
                except BrowserRenderingError as exc:
                    log.warning("RC room render failed for %s: %s", cabin_class, exc)
                    continue
                found = self.parse_room_html(html, category, try_url)
                if found:
                    return cabin_class, found
            return cabin_class, []

        with ThreadPoolExecutor(max_workers=len(CABIN_CLASSES)) as pool:
            return {cls: rooms for cls, rooms in pool.map(one, CABIN_CLASSES) if rooms}

    def merge_rooms(self, class_rows: list[Stateroom], detail: dict[str, list[Stateroom]],
                    warnings: list[str]) -> list[Stateroom]:
        """Room types where the room pages loaded, else the class's lead-in fare, in class order."""
        by_class = {r.code: r for r in class_rows}
        out: list[Stateroom] = []
        for cabin_class, category in CABIN_CLASSES.items():
            row = by_class.get(cabin_class)
            rooms = detail.get(cabin_class)
            if rooms:
                for r in rooms:
                    if row is not None:
                        r.taxes_fees_per_person = row.taxes_fees_per_person
                out.extend(rooms)
            elif row is not None:
                out.append(row)
            else:
                warnings.append(f"No {category} prices found on Royal Caribbean (sold out or page changed).")
        return out

    @staticmethod
    def parse_room_html(html: str, category: str, source: str) -> list[Stateroom]:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("[data-testid='guarantee-content-container'], div[class*='Room_content']")
        rooms = []
        for c in containers:
            title = c.select_one("[data-testid='card-title']")
            if not title:
                continue
            name = title.get_text(strip=True)
            if "we choose" in name.lower():  # RC's guarantee cabins
                continue
            price_el = c.select_one("[data-testid='room-details-price'] [data-testid='main-price-amount']")
            price = parse_price(price_el.get_text(strip=True) if price_el else None)
            rooms.append(
                Stateroom(
                    category=category,
                    name=name,
                    code=None,
                    price_per_person=price,
                    taxes_fees_per_person=None,
                    sold_out=price is None,
                    notes=None,
                    source=source,
                )
            )
        return rooms

    # ── Add-ons ──────────────────────────────────────────────────────────

    def _graphql(self, variables: dict) -> Optional[dict]:
        body = {"operationName": "WebProductsByCategory", "variables": variables, "query": PRODUCT_QUERY}
        for _ in range(3):
            try:
                resp = self.http.post(self.graphql_url, json=body, headers=self.graphql_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if "errors" not in data:
                        return data
            except httpx.HTTPError:
                pass
        return None

    def fetch_addons(self, ship_code: str, sail_date: str, warnings: list[str]) -> list[AddOn]:
        addons: list[AddOn] = []
        failed = []
        for category, kind, paginated in self.addon_categories:
            page, total_pages = 0, 1
            while page < total_pages:
                data = self._graphql(
                    {
                        "category": category,
                        "passengerId": "",
                        "shipCode": ship_code,
                        "sailDate": sail_date,
                        "reservationId": None,
                        "pageSize": 24 if paginated else 20,
                        "currentPage": page,
                        "sorting": {"sortKey": "RANK", "sortKeyOrder": "ASCENDING"},
                        "filter": {"includeVariantProducts": False},
                        "currencyCode": "USD",
                    }
                )
                if data is None:
                    failed.append(category)
                    break
                products = ((data.get("data") or {}).get("products")) or {}
                addons.extend(self.map_products(products.get("commerceProducts") or [], kind, self.addon_source))
                total_pages = ((products.get("pageInfo") or {}).get("totalPages")) or 1 if paginated else 1
                page += 1
        if failed:
            warnings.append(f"Couldn't load {self.brand_label} add-ons for: {', '.join(failed)}.")
        return addons

    @staticmethod
    def map_products(products: list[dict], kind: str, source: str = "Royal Caribbean Cruise Planner") -> list[AddOn]:
        out = []
        for p in products:
            prices = p.get("price") or []
            if not prices:
                continue
            price = prices[0]
            label = ((price.get("salesUnit") or {}).get("label")) or None
            promo = (p.get("promotion") or {}).get("displayName") if p.get("promotion") else None
            ports = p.get("dayPorts") or []
            port = ports[0].get("port") if ports and kind in {"excursion", "dining"} else None
            out.append(
                AddOn(
                    kind=kind,
                    name=p.get("title") or "",
                    price=parse_price(price.get("formattedPromotionalPrice") or price.get("formattedBasePrice")),
                    price_unit=unit_from_label(label),
                    unit_label=label,
                    port=port,
                    description=f"Promotion: {promo}" if promo else None,
                    source=source,
                )
            )
        return out
