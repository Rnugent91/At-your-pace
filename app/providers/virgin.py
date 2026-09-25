"""Virgin Voyages: live fares straight from the virginvoyages.com booking flow.

The "Book a Voyage" app (a Next.js site) talks to three public services, all
usable with the anonymous *guest* token the site hands every visitor:

  * guest token      — GET  https://www.virginvoyages.com/book/api/auth?tokenType=guest
  * sailing lookup   — POST https://prod.virginvoyages.com/bookvoyage-bff/v2/voyages
                       (voyage search: sailings with ship code, ports and the
                       lowest fare + taxes for the party)
  * itinerary        — GET  .../bookvoyage-bff/sailings?voyageId=&packageCode=
                       (day-by-day ports with arrival/departure times, ship name)
  * stateroom fares  — POST https://prod.virginvoyages.com/graphql
                       (CabinCategoriesAvailability: every cabin category with
                       Base / Essential / Premium fare totals for the party)

One voyage search over a wide date range returns every bookable sailing of the
line (~420 in Sept 2026) in a single ~2s call; the catalog then makes one fast
GraphQL call per sailing (~0.1s each) for per-class lead-in fares.

All of this works directly from server IPs (verified Sept 2026). If Virgin starts blocking them,
the same calls are replayed from inside a real browser via Cloudflare Browser
Rendering (when configured), like the Royal Caribbean provider does.

Prices from Virgin are totals for the whole cabin, excluding taxes (verified
against the site's "from $903 per Sailor" = 1806 / 2); we divide by the number
of sailors to get the per-person figure the quote expects.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest

log = logging.getLogger(__name__)

SITE = "https://www.virginvoyages.com"
AUTH_URL = f"{SITE}/book/api/auth?tokenType=guest"
BFF_URL = "https://prod.virginvoyages.com/bookvoyage-bff"
GRAPHQL_URL = "https://prod.virginvoyages.com/graphql"
# Page loaded in the browser fallback (same origin as the auth endpoint).
BROWSER_PAGE = f"{SITE}/book/voyage-planner/find-a-voyage"

# Ship name → Virgin ship code (the first two letters of every voyage id, e.g.
# "BR26113010NDP"). Verified against the ship names Virgin's /sailings returns;
# the lookup re-checks the name so a wrong entry fails loudly.
SHIP_CODES = {
    "scarlet lady": "SC",
    "valiant lady": "VL",
    "resilient lady": "RS",
    "brilliant lady": "BR",
}

# Virgin "generic category" (cabinCategoriesAvailability.code) → our category.
CATEGORY_MAP = {
    "INSIDER": "Interior",
    "SEA VIEW": "Ocean View",
    "SEA TERRACE": "Balcony",
    "ROCKSTAR SUITES": "Suite",
    "MEGA ROCKSTAR": "Suite",
}

FARE_CLASS_NOTES = {"ESSENTIAL": "Essential fare", "PREMIUM": "Premium fare"}

FARE_CLASS_WARNING = (
    "Prices are Virgin's lowest (Base) fare for each cabin. Room notes show the Essential and Premium "
    "fares where offered — these add upgraded Wi-Fi, more flexibility and (Premium) extra perks; check "
    "the current inclusions on virginvoyages.com before quoting them."
)

INCLUDED_NOTE = (
    "Virgin Voyages fares include basic Wi-Fi, all restaurants, basic drinks (soda, still/sparkling "
    "water, coffee/tea), group fitness, entertainment and crew gratuities. Alcohol is extra: a prepaid "
    "Bar Tab is an optional add-on (not priced by the live integration)."
)

# CatalogSailing.prices class names, in display order.
CATALOG_CLASSES = ("Interior", "Ocean View", "Balcony", "Suite")
CATALOG_SAILORS = 2

CABIN_QUERY = """
query CabinCategoriesAvailability($value: CabinCategoriesAvailabilityRequest!) {
  cabinCategoriesAvailability(value: $value) {
    cabinSeqNo
    availableCategories {
      code
      name
      submetas {
        code
        name
        isAvailable
        attributes
        lowestAvailablePrice { fareClass totalPrice { amount discount currencyCode originalAmount } }
        fareClassContents { fareClass totalPrice { amount currencyCode originalAmount } }
      }
    }
  }
}
"""


def ship_code_for(ship: str) -> Optional[str]:
    s = re.sub(r"\s+", " ", ship.strip().lower())
    if re.fullmatch(r"[a-z]{2}", s):
        return s.upper() if s.upper() in SHIP_CODES.values() else None
    s = s.removeprefix("the ")
    if s in SHIP_CODES:
        return SHIP_CODES[s]
    if f"{s} lady" in SHIP_CODES:  # "Scarlet" → Scarlet Lady
        return SHIP_CODES[f"{s} lady"]
    return None


def parse_voyage_id(voyage_id: str) -> Optional[tuple[str, str]]:
    """'SC2803117NSJRP' → ('SC', '2028-03-11'): ship code + yymmdd sail date."""
    m = re.match(r"([A-Z]{2})(\d{2})(\d{2})(\d{2})", (voyage_id or "").strip().upper())
    if not m:
        return None
    try:
        return m[1], date(2000 + int(m[2]), int(m[3]), int(m[4])).isoformat()
    except ValueError:
        return None


def voyage_from_url(url: str) -> Optional[str]:
    """The voyageId from a virginvoyages.com booking URL, if present."""
    if not url or "virginvoyages" not in url.lower():
        return None
    qs = parse_qs(urlparse(url).query)
    vid = (qs.get("voyageId") or qs.get("voyageid") or [None])[0]
    if not vid:
        m = re.search(r"(?i)voyageId=([A-Z0-9]+)", url)
        vid = m[1] if m else None
    return vid.upper() if vid and parse_voyage_id(vid) else None


def search_payload(start: str, end: str, sailors: int) -> dict:
    return {
        "sailingsGroupByPackage": True,
        "searchQualifier": {
            "accessKeys": [],
            "cabins": [{"guestCounts": [{"ageCategory": "Adult", "count": sailors}]}],
            "classificationCodes": [],
            "currencyCode": "USD",
            "preferences": [],
            "sailingDateRange": [{"start": start, "end": end}],
        },
    }


def iter_search_sailings(data: dict) -> Iterator[dict]:
    """Every sailing in a voyage-search response, with its package name merged in."""
    for group in ("packages", "defaultPackages"):
        for pkg in data.get(group) or []:
            for s in pkg.get("sailingList") or []:
                yield {"packageName": pkg.get("packageName"), **s}


def ports_of_call(search_ports: list[dict]) -> list[str]:
    """Search-result ports minus embarkation/debarkation days, consecutive repeats merged."""
    ports = sorted((p for p in search_ports if (p.get("name") or "").strip()), key=lambda p: int(p.get("day") or 0))
    out: list[str] = []
    for p in ports[1:-1]:
        name = p["name"].strip()
        if not out or out[-1] != name:
            out.append(name)
    return out


def to_24h(t: Optional[str]) -> Optional[str]:
    """'05:00 PM' → '17:00'. Passes through anything already 24-hour."""
    if not t:
        return None
    t = t.strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(t.upper(), fmt).strftime("%H:%M")
        except ValueError:
            continue
    return t


def _amount(price: Optional[dict]) -> Optional[float]:
    if not price:
        return None
    total = price.get("totalPrice") if "totalPrice" in price else price
    value = (total or {}).get("amount")
    return float(value) if value is not None else None


class VirginVoyagesProvider:
    name = "Virgin Voyages (live)"
    cruise_line = "Virgin Voyages"

    def __init__(self, cf: Optional[CloudflareBrowser] = None, http: Optional[httpx.Client] = None):
        self.cf = cf
        self.http = http or httpx.Client(timeout=45, headers={"user-agent": USER_AGENT})
        self.calls = 0  # HTTP API calls made (for catalog stats)
        self._token: Optional[str] = None
        self._token_expires = 0.0

    def handles(self, cruise_line: str) -> bool:
        return "virgin" in cruise_line.lower()

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        code = ship_code_for(req.ship)
        sail_date = req.sail_date
        url_voyage = voyage_from_url(req.booking_url)
        if url_voyage:  # a pasted booking URL pins the exact sailing
            code, sail_date = parse_voyage_id(url_voyage)
        if not code:
            raise ProviderError(
                f"Unknown Virgin Voyages ship '{req.ship}' — expected one of "
                + ", ".join(n.title() for n in SHIP_CODES)
            )
        sailors = max(1, req.adults + req.children)

        sailing = self.find_sailing(code, sail_date, sailors, voyage_id=url_voyage)
        if not sailing:
            raise ProviderError(f"No Virgin Voyages sailing found for {req.ship or code} on {sail_date}")
        voyage_id, package_code = sailing["id"], sailing["packageCode"]
        booking_url = f"{SITE}/book/voyage-planner/choose-a-cabin?" + urlencode(
            {"packageCode": package_code, "voyageId": voyage_id, "cabins": 1, "sailors": sailors, "currencyCode": "USD"}
        )

        warnings: list[str] = [INCLUDED_NOTE]
        if req.children:
            warnings.append("Virgin Voyages is adults-only (18+); children were priced as adult sailors.")

        details: dict = {}
        try:
            details = self.fetch_sailing_details(voyage_id, package_code)
        except ProviderError as exc:
            warnings.append(f"Couldn't load the Virgin itinerary details: {exc}")

        ship_name = (details.get("ship") or {}).get("name") or next(
            (n.title() for n, c in SHIP_CODES.items() if c == code), req.ship
        )
        if ship_code_for(ship_name) not in (code, None):
            raise ProviderError(f"Virgin ship code {code} is {ship_name}, not {req.ship}")
        if url_voyage and req.ship and ship_code_for(req.ship) not in (code, None):
            warnings.append(f"The booking link is for {ship_name}, not {req.ship}; priced the linked sailing.")
        if sail_date != req.sail_date:
            warnings.append(f"The booking link's sailing departs {sail_date}, not {req.sail_date}.")

        ports = sailing.get("ports") or []
        research = SailingResearch(
            cruise_line="Virgin Voyages",
            ship=ship_name,
            sail_date=sail_date,
            nights=sailing.get("duration") or details.get("duration"),
            departure_port=(ports[0].get("name") or "").strip() or None if ports else None,
            itinerary_name=details.get("name") or sailing.get("packageName"),
            itinerary=self.map_itinerary(details.get("itinerary") or [], ports, sail_date),
            currency=(sailing.get("startingPrice") or {}).get("currencyCode") or "USD",
            staterooms=[],
            addons=[],
            sources=[booking_url],
            warnings=warnings,
        )
        warnings = research.warnings  # pydantic copies lists passed in

        start = sailing.get("startingPrice") or {}
        tax = start.get("taxAmount")
        tax_pp = round(float(tax) / sailors, 2) if tax is not None else None
        try:
            categories = self.fetch_cabin_categories(voyage_id, sailors)
            research.staterooms = self.map_staterooms(categories, sailors, tax_pp, booking_url)
        except ProviderError as exc:
            warnings.append(f"Stateroom prices could not be loaded from Virgin Voyages: {exc}")
        if any(r.notes and "Premium fare" in r.notes for r in research.staterooms):
            warnings.append(FARE_CLASS_WARNING)
        if research.staterooms and tax_pp is not None:
            warnings.append(
                "Virgin shows one taxes & fees figure per voyage; it was applied to every cabin category."
            )
        return research

    # ── Transport ────────────────────────────────────────────────────────

    def _guest_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        try:
            self.calls += 1
            resp = self.http.get(AUTH_URL, headers={"accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"couldn't get a Virgin guest token ({exc})") from exc
        token = data.get("access_token")
        if not token:
            raise ProviderError("Virgin guest token response had no access_token")
        self._token = token
        self._token_expires = time.time() + max(60, int(data.get("expires_in") or 3600) - 300)
        return token

    def _request(self, method: str, url: str, *, params: Optional[dict] = None, body: Any = None) -> Any:
        """Call a Virgin API directly; fall back to an in-browser fetch if we're blocked."""
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            resp = None
            for attempt in range(4):
                self.calls += 1
                resp = self.http.request(
                    method,
                    url,
                    json=body,
                    headers={
                        "authorization": f"bearer {self._guest_token()}",
                        "accept": "application/json",
                        "origin": SITE,
                        "referer": BROWSER_PAGE,
                    },
                )
                if resp.status_code in (401, 403) and attempt == 0:
                    self._token = None  # token revoked or expired early: retry once with a fresh one
                    continue
                if resp.status_code in (429, 502, 503, 504) and attempt < 3:
                    retry_after = resp.headers.get("retry-after", "")
                    time.sleep(min(30.0, float(retry_after)) if retry_after.isdigit() else 2.0 * (attempt + 1))
                    continue
                break
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content.strip():
                return {}  # voyage search answers 204 No Content when nothing matches
            return resp.json()
        except (httpx.HTTPError, ValueError, ProviderError) as exc:
            if self.cf is None or not self.cf.configured:
                raise ProviderError(f"Virgin Voyages request failed ({exc})") from exc
            log.warning("Virgin direct call failed (%s); retrying through Cloudflare browser", exc)
            return self._request_via_browser(method, url, body)

    def _request_via_browser(self, method: str, url: str, body: Any) -> Any:
        # Load a virginvoyages.com page, fetch a guest token from its own auth
        # route and make the API call from inside it, then stash the JSON in a
        # DOM node we can read back from the rendered HTML.
        init = "{method:'" + method + "',headers:{'content-type':'application/json','accept':'application/json','authorization':'bearer '+t}"
        if body is not None:
            init += ",body:" + json.dumps(json.dumps(body))
        init += "}"
        script = (
            "(async function(){var out;try{var t=(await fetch('/book/api/auth?tokenType=guest')"
            ".then(function(r){return r.json();})).access_token;"
            "out=await fetch(" + json.dumps(url) + "," + init + ").then(function(r){return r.text();});}"
            "catch(e){out='ERR:'+(e&&e.message||e);}"
            "var d=document.createElement('div');d.id='vv-json';"
            "d.setAttribute('data-json',encodeURIComponent(out));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content(
                {
                    "url": BROWSER_PAGE,
                    "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                    "addScriptTag": [{"content": script}],
                    "waitForSelector": {"selector": "#vv-json", "timeout": 45000},
                }
            )
        except BrowserRenderingError as exc:
            raise ProviderError(f"Virgin Voyages browser request failed: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="vv-json")
        if node is None:
            raise ProviderError("Virgin Voyages browser request returned no data")
        text = unquote(node.get("data-json", ""))
        if text.startswith("ERR:"):
            raise ProviderError(f"Virgin Voyages request failed in-page: {text[:200]}")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ProviderError(f"Virgin Voyages returned non-JSON: {text[:200]}") from exc

    # ── Sailing lookup ───────────────────────────────────────────────────

    def search(self, start: str, end: str, sailors: int = CATALOG_SAILORS) -> dict:
        """Voyage search: every sailing departing in (roughly) [start, end]."""
        return self._request("POST", f"{BFF_URL}/v2/voyages", body=search_payload(start, end, sailors))

    def find_sailing(
        self, ship_code: str, sail_date: str, sailors: int, voyage_id: Optional[str] = None
    ) -> Optional[dict]:
        """The voyage-search sailing for this ship leaving on `sail_date` (with its package fields)."""
        day = date.fromisoformat(sail_date)
        data = self.search((day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat(), sailors)
        return self.pick_sailing(data, ship_code, sail_date, voyage_id)

    @staticmethod
    def pick_sailing(data: dict, ship_code: str, sail_date: str, voyage_id: Optional[str] = None) -> Optional[dict]:
        matches = [
            s
            for s in iter_search_sailings(data)
            if (s.get("shipCode") or (s.get("id") or "")[:2]) == ship_code
            and (s.get("startDate") or "")[:10] == sail_date
        ]
        if voyage_id:
            return next((s for s in matches if s.get("id") == voyage_id), matches[0] if matches else None)
        return matches[0] if matches else None

    def fetch_sailing_details(self, voyage_id: str, package_code: str) -> dict:
        return self._request("GET", f"{BFF_URL}/sailings", params={"voyageId": voyage_id, "packageCode": package_code})

    @staticmethod
    def map_itinerary(days: list[dict], search_ports: list[dict], sail_date: str) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        # The search result has fuller port names ("Philipsburg, St. Maarten").
        full_names = {p.get("code"): (p.get("name") or "").strip() for p in search_ports if p.get("code")}
        out = []
        if days:
            for d in days:
                n = int(d.get("itineraryDay") or d.get("index") or len(out) + 1)
                sea = bool(d.get("seaDay")) or (d.get("dayType") or "").lower() == "sailing"
                name = full_names.get(d.get("portCode")) or (d.get("portName") or "").strip() or "Port"
                out.append(
                    ItineraryDay(
                        day=n,
                        date=(start + timedelta(days=n - 1)).isoformat(),
                        port="At Sea" if sea else name,
                        arrive=None if sea else to_24h(d.get("arrivalTime")),
                        depart=None if sea else to_24h(d.get("departureTime")),
                    )
                )
            return out
        # No itinerary endpoint: build from the search's port list, filling sea days.
        by_day = {int(p["day"]): p for p in search_ports if p.get("day")}
        for n in range(1, max(by_day, default=0) + 1):
            p = by_day.get(n)
            out.append(
                ItineraryDay(
                    day=n,
                    date=(start + timedelta(days=n - 1)).isoformat(),
                    port=(p.get("name") or "").strip() or "Port" if p else "At Sea",
                    arrive=None,
                    depart=None,
                )
            )
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    def fetch_cabin_categories(self, voyage_id: str, sailors: int) -> list[dict]:
        body = {
            "query": CABIN_QUERY,
            "variables": {
                "value": {
                    "accessKeys": [],
                    "cabins": [
                        {
                            "cabinSeqNo": 1,
                            "isAccessible": False,
                            "travelParty": [{"ageCategory": "ADULT", "count": sailors}],
                        }
                    ],
                    "currencyCode": "USD",
                    "voyageId": voyage_id,
                }
            },
        }
        data = self._request("POST", GRAPHQL_URL, body=body)
        if data.get("errors") and not data.get("data"):
            raise ProviderError(f"cabin availability error: {str(data['errors'])[:200]}")
        cabins = ((data.get("data") or {}).get("cabinCategoriesAvailability")) or []
        return (cabins[0].get("availableCategories") if cabins else None) or []

    @staticmethod
    def map_staterooms(categories: list[dict], sailors: int, tax_pp: Optional[float], source: str) -> list[Stateroom]:
        rooms: list[Stateroom] = []
        for cat in categories:
            category = CATEGORY_MAP.get((cat.get("code") or "").upper(), "Other")
            group = cat.get("name") or cat.get("code") or ""
            seen: set[str] = set()
            found = []
            for sm in cat.get("submetas") or []:
                code = sm.get("code")
                if not code or code in seen or not sm.get("isAvailable"):
                    continue  # Solo/Social cabins show as unavailable for other party sizes
                seen.add(code)
                total = _amount(sm.get("lowestAvailablePrice"))
                fares = {f.get("fareClass"): _amount(f) for f in sm.get("fareClassContents") or []}
                notes = []
                attrs = [a.strip().rstrip(":") for a in sm.get("attributes") or [] if a and a.strip().rstrip(":")]
                if attrs:
                    notes.append("; ".join(attrs[:3]))
                for fc in ("ESSENTIAL", "PREMIUM"):
                    if fares.get(fc) is not None:
                        notes.append(f"{FARE_CLASS_NOTES[fc]}: ${fares[fc] / sailors:,.0f} pp")
                orig = ((sm.get("lowestAvailablePrice") or {}).get("totalPrice") or {}).get("originalAmount")
                if orig and total and orig > total:
                    notes.append(f"was ${orig / sailors:,.0f} pp")
                name = (sm.get("name") or code).strip()
                if group and group.lower() not in name.lower():
                    name = f"{name} ({group})"
                found.append(
                    Stateroom(
                        category=category,
                        name=name,
                        code=code,
                        price_per_person=round(total / sailors, 2) if total is not None else None,
                        taxes_fees_per_person=tax_pp,
                        sold_out=total is None,
                        notes=" · ".join(notes) or None,
                        source=source,
                    )
                )
            if not found:
                found.append(
                    Stateroom(
                        category=category,
                        name=group,
                        code=cat.get("code"),
                        price_per_person=None,
                        taxes_fees_per_person=None,
                        sold_out=True,
                        notes="No cabins available in this category for this party.",
                        source=source,
                    )
                )
            rooms.extend(sorted(found, key=lambda r: (r.price_per_person is None, r.price_per_person or 0)))
        return rooms

    # ── Catalog ──────────────────────────────────────────────────────────

    @staticmethod
    def class_prices(categories: list[dict], sailors: int = CATALOG_SAILORS) -> dict[str, Optional[float]]:
        """Lead-in fare per person for each room class (None = class listed but sold out)."""
        prices: dict[str, Optional[float]] = {}
        for cat in categories:
            cls = CATEGORY_MAP.get((cat.get("code") or "").upper())
            if not cls:
                continue
            for sm in cat.get("submetas") or []:
                total = _amount(sm.get("lowestAvailablePrice")) if sm.get("isAvailable") and sm.get("code") else None
                pp = round(total / sailors, 2) if total else None
                old = prices.get(cls)
                prices[cls] = pp if old is None else (old if pp is None else min(old, pp))
            prices.setdefault(cls, None)
        return {c: prices[c] for c in CATALOG_CLASSES if c in prices}

    def catalog_row(self, sailing: dict, categories: Optional[list[dict]]) -> CatalogSailing:
        code = sailing.get("shipCode") or sailing["id"][:2]
        start = sailing.get("startingPrice") or {}
        tax = start.get("taxAmount")
        if categories:
            prices = self.class_prices(categories)
        else:  # cabin call failed: at least keep the search's overall lead-in
            cls = CATEGORY_MAP.get((start.get("minPricedGenericCategoryCode") or "").upper())
            prices = {cls: round(float(start["amount"]) / CATALOG_SAILORS, 2)} if cls and start.get("amount") else {}
        ports = sailing.get("ports") or []
        return CatalogSailing(
            cruise_line=self.cruise_line,
            sailing_key=sailing["id"],
            ship=next((n.title() for n, c in SHIP_CODES.items() if c == code), code),
            sail_date=(sailing.get("startDate") or "")[:10],
            nights=sailing.get("duration"),
            ship_code=code,
            itinerary_name=sailing.get("packageName"),
            departure_port=(ports[0].get("name") or "").strip() or None if ports else None,
            ports=ports_of_call(ports),
            booking_url=f"{SITE}/book/voyage-planner/choose-a-cabin?"
            + urlencode(
                {
                    "packageCode": sailing.get("packageCode"),
                    "voyageId": sailing["id"],
                    "cabins": 1,
                    "sailors": CATALOG_SAILORS,
                    "currencyCode": "USD",
                }
            ),
            prices=prices,
            taxes_fees_per_person=round(float(tax) / CATALOG_SAILORS, 2) if tax is not None else None,
            currency=start.get("currencyCode") or "USD",
        )

    def iter_catalog(self, today: Optional[date] = None, workers: int = 6, years: int = 4) -> Iterator[CatalogSailing]:
        """Every future bookable Virgin sailing with lead-in fares per room class.

        A handful of yearly voyage searches (one call already returns the whole line;
        the split just guards against a server-side cap), then one GraphQL cabin call
        per sailing, a few at a time.
        """
        today = today or date.today()
        sailings: dict[str, dict] = {}
        for y in range(years):
            start = today + timedelta(days=365 * y)
            end = start + timedelta(days=364)
            for s in iter_search_sailings(self.search(start.isoformat(), end.isoformat())):
                if s.get("id") and (s.get("startDate") or "")[:10] >= today.isoformat():
                    sailings.setdefault(s["id"], s)
        ordered = sorted(sailings.values(), key=lambda s: (s["startDate"], s["id"]))

        def cabins(s: dict) -> Optional[list[dict]]:
            try:
                return self.fetch_cabin_categories(s["id"], CATALOG_SAILORS)
            except ProviderError as exc:
                log.warning("Virgin cabin prices failed for %s: %s", s["id"], exc)
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for sailing, cats in zip(ordered, pool.map(cabins, ordered)):
                yield self.catalog_row(sailing, cats)
