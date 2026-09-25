"""Carnival Cruise Line: live prices from carnival.com's own JSON APIs.

Endpoints (the ones carnival.com's pages call; found by capturing the site's
XHR traffic, with the cruise search first spotted in the "cruisekit" project):
  * sailing lookup  — GET  /cruisesearch/api/search            (ship + month filter)
  * itinerary       — GET  /cruisesearch/api/search/itinerary  (day-by-day schedule for one sail date)
  * staterooms      — POST /booking-api/api/v1.0/book          (the booking flow's room picker:
                       one call per room type returns every stateroom category with its price)
  * add-ons         — GET  /shop/plp/search/<category|port>    (FunShops: drinks, Wi-Fi, shore excursions)

All of these answer plain server requests today. If Akamai starts blocking a
server IP, the same request is replayed from inside a real browser through
Cloudflare Browser Rendering (when configured).

Prices: carnival.com (US) now shows all-in prices — the search API reports
`taxesIncludedUSEnabled: true` and the site prints "* Taxes & fees are included".
The booking API's per-guest breakdown confirms it: shown price = fare +
`cruiseFeesAndExpenses` (port expenses) + `taxesAndFees` (e.g. $564 = $369 +
$100.56 + $94.44). We report the fare excluding both, and their sum as
`taxes_fees_per_person`.
"""

import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Iterator, Optional
from urllib.parse import unquote, urlencode

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest
from .royal_caribbean import unit_from_label

log = logging.getLogger(__name__)

BASE_URL = "https://www.carnival.com"

# Ship name → Carnival ship code, as listed by carnival.com's search filters and
# /shop/ship. The lookup checks the ship name Carnival returns, so a wrong entry
# fails loudly. Advisors can also type the two-letter code directly.
SHIP_CODES = {
    "carnival adventure": "AQ",
    "carnival breeze": "BR",
    "carnival celebration": "CB",
    "carnival conquest": "CQ",
    "carnival dream": "DR",
    "carnival elation": "EL",
    "carnival encounter": "EQ",
    "carnival festivale": "FT",
    "carnival firenze": "FN",
    "carnival freedom": "FD",
    "carnival glory": "GL",
    "carnival horizon": "HZ",
    "carnival jubilee": "JB",
    "carnival legend": "LE",
    "carnival liberty": "LI",
    "carnival luminosa": "LM",
    "carnival magic": "MC",
    "carnival miracle": "MI",
    "carnival panorama": "PO",
    "carnival paradise": "PA",
    "carnival pride": "PR",
    "carnival radiance": "RD",
    "carnival spirit": "SP",
    "carnival splendor": "SL",
    "carnival sunrise": "SN",
    "carnival sunshine": "SH",
    "carnival tropicale": "TL",
    "carnival valor": "VA",
    "carnival venezia": "VX",
    "carnival vista": "VS",
    "mardi gras": "MD",
}

# Booking-flow "meta" (room type) codes → our category.
META_CATEGORIES = {
    "IS": "Interior",
    "OS": "Ocean View",
    "OB": "Balcony",
    "SU": "Suite",
}

# Search results' per-sailing "from" prices, used if the booking flow fails.
SEARCH_ROOM_KEYS = {
    "interior": "Interior",
    "oceanview": "Ocean View",
    "balcony": "Balcony",
    "suite": "Suite",
}

# FunShops categories with fleet-wide published prices. (Specialty dining is
# left out: without a booking the list is every ship's cooking classes.)
# Specialty-dining venue → words the ship's page uses for it (Carnival's dining shop
# lists every ship's venues, with duplicate codes per ship class).
CARNIVAL_KITCHEN = ["carnival kitchen"]
DINING_VENUES = [
    (r"steakhouse", ["steakhouse"]),
    (r"teppanyaki", ["teppanyaki"]),
    (r"seagrill", ["seagrill"]),
    (r"ji ji|jiji", ["jiji", "ji ji"]),
    (r"chefs table", ["chef's table", "chefs table", "chef s table"]),
    (r"cucina del capitano", ["cucina del capitano"]),
    (r"il viaggio", ["il viaggio"]),
    (r"thing 1", ["seuss", "green eggs"]),
    (r"emeril", ["emeril"]),
    (r"class|workshop|academy", CARNIVAL_KITCHEN),
]
# Excel-class ships, which have Carnival Kitchen (their ship pages don't name it).
EXCEL_SHIPS = {"MD", "CB", "JB"}


def ship_code_for(ship: str) -> Optional[str]:
    s = ship.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    key = re.sub(r"\s+", " ", s.lower())
    if key in SHIP_CODES:
        return SHIP_CODES[key]
    if not key.startswith("carnival ") and f"carnival {key}" in SHIP_CODES:
        return SHIP_CODES[f"carnival {key}"]
    return None


def mmddyyyy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}{d}{y}"


def _clock(ts: Optional[str]) -> Optional[str]:
    """'2026-11-07T15:30:00+00:00' → '15:30' (Carnival sends local port time labelled +00:00)."""
    if not ts or "T" not in ts:
        return None
    return ts.split("T", 1)[1][:5]


def is_sea_day(stop: dict) -> bool:
    port = (stop.get("port") or "").lower()
    return not port or "at sea" in port or "sea day" in port or (stop.get("portCode") or "").upper().startswith("FS")


def port_slugs(name: str) -> list[str]:
    """Candidate FunShops category slugs for a port, e.g. 'RelaxAway, Half Moon Cay™' →
    ['relaxaway-half-moon-cay', 'relaxaway', 'half-moon-cay']."""
    clean = re.sub(r"[™®]", "", name)

    def slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

    out = [slug(clean)] + [slug(part) for part in clean.split(",")]
    no_parens = re.sub(r"\(.*?\)", "", clean)
    out.append(slug(no_parens))
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _strip_html(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


class CarnivalProvider:
    name = "Carnival (live)"
    cruise_line = "Carnival"

    def __init__(self, cf: Optional[CloudflareBrowser] = None, http: Optional[httpx.Client] = None,
                 base_url: str = BASE_URL):
        self.cf = cf
        self.http = http or httpx.Client(timeout=30, follow_redirects=True)
        self.base_url = base_url.rstrip("/")
        self._blocked = False  # set once carnival.com refuses server requests; later calls go via CF
        self._lock = threading.Lock()
        self._sleep = time.sleep
        self._today = date.today
        self.http_calls = 0  # direct HTTP requests made (for catalog run stats)

    def handles(self, cruise_line: str) -> bool:
        return "carnival" in cruise_line.lower()

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _json(self, method: str, path: str, body: Optional[dict] = None, attempts: int = 4):
        """Call a carnival.com JSON endpoint directly (retrying 429/5xx with backoff),
        or from inside a browser via Cloudflare if carnival.com blocks this server."""
        cf_ok = self.cf is not None and self.cf.configured
        attempt = 0
        while not self._blocked:
            attempt += 1
            with self._lock:
                self.http_calls += 1
            try:
                resp = self.http.request(
                    method,
                    self.base_url + path,
                    json=body,
                    headers={
                        "accept": "application/json",
                        "origin": self.base_url,
                        "referer": self.base_url + "/",
                        "user-agent": USER_AGENT,
                    },
                )
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    self._sleep(2 ** (attempt - 1))
                    continue
                if not cf_ok:
                    raise ProviderError(f"Carnival request {path} failed: {exc}") from exc
                log.warning("Carnival direct call %s failed (%s); trying Cloudflare browser", path, exc)
                break
            status = resp.status_code
            if status in (401, 403) and cf_ok:
                log.warning("carnival.com refused %s (%s); switching to Cloudflare browser", path, status)
                self._blocked = True
                break
            if (status == 429 or status >= 500) and attempt < attempts:
                retry_after = resp.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** (attempt - 1)
                self._sleep(min(delay, 30))
                continue
            if status == 429 and cf_ok:
                self._blocked = True
                break
            try:
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise ProviderError(f"Carnival request {path} failed: {exc}") from exc
        return self._json_via_browser(method, path, body)

    def _json_via_browser(self, method: str, path: str, body: Optional[dict]):
        if self.cf is None or not self.cf.configured:
            raise ProviderError("carnival.com blocked the request. Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN.")
        opts = {"method": method, "headers": {"accept": "application/json", "content-type": "application/json"}}
        opts_js = json.dumps(opts)
        if body is not None:
            opts_js = opts_js[:-1] + ',"body":' + json.dumps(json.dumps(body)) + "}"
        script = (
            "(async function(){var out;try{out=await fetch(" + json.dumps(path) + "," + opts_js + ")"
            ".then(function(r){return r.text();});}catch(e){out='ERR:'+(e&&e.message||e);}"
            "var d=document.createElement('div');d.id='qp-carnival';"
            "d.setAttribute('data-json',encodeURIComponent(out));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content(
                {
                    "url": self.base_url + "/cruise-search",
                    "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                    "addScriptTag": [{"content": script}],
                    "waitForSelector": {"selector": "#qp-carnival", "timeout": 55000},
                }
            )
        except BrowserRenderingError as exc:
            raise ProviderError(f"Carnival request {path} failed in browser: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="qp-carnival")
        if node is None:
            raise ProviderError(f"Carnival request {path} returned no data")
        text = unquote(node.get("data-json", ""))
        if text.startswith("ERR:"):
            raise ProviderError(f"Carnival request {path} failed in-page: {text[:200]}")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ProviderError(f"Carnival request {path} returned non-JSON: {text[:120]}") from exc

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        code = ship_code_for(req.ship)
        if not code:
            raise ProviderError(f"Unknown Carnival ship '{req.ship}' — enter its two-letter Carnival code")
        guests = max(1, req.adults + req.children)

        found = self.find_sailing(code, req.sail_date, guests)
        if not found:
            raise ProviderError(f"No Carnival sailing found for ship {code} on {req.sail_date}")
        itin, sailing = found
        ship_name = itin.get("shipName") or req.ship
        if ship_code_for(ship_name) not in (code, None):
            raise ProviderError(f"Carnival code {code} is {ship_name}, not {req.ship}")

        research = SailingResearch(
            cruise_line="Carnival",
            ship=ship_name,
            sail_date=req.sail_date,
            nights=itin.get("dur"),  # Carnival's "7-Day" = 7 nights (sail date to return date)
            departure_port=itin.get("departurePortName"),
            itinerary_name=itin.get("itineraryTitleFormatted") or itin.get("itineraryTitle"),
            itinerary=[],
            currency=(sailing.get("rooms") or {}).get("interior", {}).get("priceCurrency") or "USD",
            staterooms=[],
            addons=[],
            sources=[f"{self.base_url}/cruise-search"],
            warnings=[],
        )
        warnings = research.warnings  # pydantic copies lists passed in, so append to the model's own

        schedule = self.fetch_schedule(itin, req.sail_date, guests)
        research.itinerary = self.map_days(schedule, req.sail_date)

        booking_url = self.base_url + sailing["sailingURL"] if sailing.get("sailingURL") else None
        addon_warnings: list[str] = []
        # Add-ons load in the background while the room picker is queried.
        addon_pool = ThreadPoolExecutor(max_workers=1)
        addon_future = addon_pool.submit(
            self.fetch_addons, research.itinerary, schedule, addon_warnings, code, ship_name,
            itin.get("dur"), itin.get("departurePortCode") or "",
        )
        book_sink: dict = {}
        try:
            research.staterooms = self.fetch_staterooms(
                sailing.get("sailingId"), req.sail_date, itin.get("dur"), code, guests, booking_url or self.base_url,
                book_sink,
            )
            if research.staterooms:
                research.sources.append(booking_url or f"{self.base_url}/booking")
                if guests != 2:
                    warnings.append(f"Carnival prices are the average per person for {guests} guests in the room.")
                if research.staterooms[0].taxes_fees_per_person is None:
                    warnings.append("Carnival didn't return its taxes breakdown; room prices INCLUDE taxes, fees "
                                    "and port expenses.")
        except ProviderError as exc:
            log.warning("Carnival stateroom fetch failed: %s", exc)
            warnings.append(f"Carnival's room picker failed ({exc}); showing search 'from' prices per room "
                            "type, which INCLUDE taxes, fees and port expenses.")
        if not research.staterooms:
            research.staterooms = self.search_rooms(sailing, booking_url or f"{self.base_url}/cruise-search")

        try:
            addons = addon_future.result()
        except Exception as exc:  # fetch_addons shouldn't raise, but never let add-ons fail research
            log.exception("Carnival add-ons failed")
            addons = []
            addon_warnings.append(f"Carnival add-ons could not be loaded: {exc}")
        finally:
            addon_pool.shutdown(wait=False)
        warnings.extend(addon_warnings)
        extras = self.booking_addons(book_sink.get("first"), itin.get("dur"), booking_url or self.base_url)
        research.addons = extras + addons
        if addons:
            research.sources.append(f"{self.base_url}/shop (FunShops)")
            warnings.append(
                "Carnival add-on prices (drinks, Wi-Fi, dining, spa, excursions) are its published 'from' prices, "
                "not quoted for this sailing; service charges are included where Carnival adds them."
            )
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def find_sailing(self, ship_code: str, sail_date: str, guests: int) -> Optional[tuple[dict, dict]]:
        month = sail_date[5:7] + sail_date[:4]  # MMYYYY
        page, last = 1, 1
        while page <= last and page <= 5:
            params = {
                "numAdults": guests,
                "pageNumber": page,
                "pageSize": 50,
                "shipCode": ship_code,
                "datFrom": month,
                "datTo": month,
                "currency": "USD",
                "locality": 1,
            }
            data = self._json("GET", "/cruisesearch/api/search?" + urlencode(params))
            results = data.get("results") or {}
            hit = self.pick_sailing(results.get("itineraries") or [], sail_date)
            if hit:
                return hit
            last = results.get("lastPage") or 1
            page += 1
        return None

    @staticmethod
    def pick_sailing(itineraries: list[dict], sail_date: str) -> Optional[tuple[dict, dict]]:
        for itin in itineraries:
            for sailing in itin.get("sailings") or []:
                if (sailing.get("departureDate") or "")[:10] == sail_date:
                    return itin, sailing
        return None

    def fetch_schedule(self, itin: dict, sail_date: str, guests: int) -> list[dict]:
        """The day-by-day schedule for this exact sail date (the search only has the lead sailing's)."""
        lead = itin.get("leadSailing") or {}
        if (lead.get("departureDate") or "")[:10] == sail_date and lead.get("schedule"):
            return lead["schedule"]
        params = {
            "currency": "USD",
            "durationDays": itin.get("dur"),
            "embarkPortCode": itin.get("departurePortCode"),
            "itineraryCode": itin.get("code"),
            "locality": 1,
            "numberOfGuests": guests,
            "shipCode": itin.get("shipCode"),
            "sailDate": mmddyyyy(sail_date),
        }
        try:
            data = self._json("GET", "/cruisesearch/api/search/itinerary?" + urlencode(params))
            for it in data.get("itineraries") or []:
                ls = it.get("leadSailing") or {}
                if (ls.get("departureDate") or "")[:10] == sail_date and ls.get("schedule"):
                    return ls["schedule"]
        except ProviderError as exc:
            log.warning("Carnival itinerary lookup failed: %s", exc)
        # Same itinerary code ⇒ same pattern; map_days re-dates it from the sail date.
        return lead.get("schedule") or []

    @staticmethod
    def map_days(schedule: list[dict], sail_date: str) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        out = []
        for i, stop in enumerate(schedule):
            n = stop.get("day") or i + 1
            sea = is_sea_day(stop)
            out.append(
                ItineraryDay(
                    day=n,
                    date=(start + timedelta(days=n - 1)).isoformat(),
                    port="At Sea" if sea else re.sub(r"[™®]", "", stop.get("port") or "Port").strip(),
                    arrive=None if sea else _clock(stop.get("arrive")),
                    depart=None if sea else _clock(stop.get("depart")),
                )
            )
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    @staticmethod
    def book_body(sailing_id, sail_date: str, dur_days, ship_code: str, guests: int, meta_code: Optional[str]) -> dict:
        return {
            "sailingId": int(sailing_id),
            "sailDate": sail_date,
            "durationDays": dur_days,
            "shipCode": ship_code,
            "currencyCode": "USD",
            "cabins": [{
                "qualifiers": {
                    "numberOfGuests": guests, "isMilitary": False, "isPastGuest": False, "pastGuestNumber": None,
                    "tierCode": None, "countryOfResidency": "US", "stateOfResidency": None, "isSenior": False,
                    "accessibility": None, "couponCode": None,
                },
                "metaCode": meta_code, "phantomMetaCode": None, "stateroomTypeCode": None, "rateCode": None,
                "price": None, "deckCode": None, "locationCode": None, "roomNumber": None,
                "cabinToRelease": None, "categoryCode": None, "upgradeFrom": None,
            }],
            "flowOption": None,
            "currentCabinIndex": 0,
            "preselectedMetaCode": None,
            "preselectedRateCodes": None,
            "sortMetasByAscPrice": False,
            "encryptedBookingNumber": None,
            "connectingCabinsIndicator": False,
            "previousCabinSelectedCategoryCode": None,
            "previousCabinSelectedRateCode": None,
        }

    def fetch_staterooms(self, sailing_id, sail_date, dur_days, ship_code, guests, source,
                         sink: Optional[dict] = None) -> list[Stateroom]:
        if not sailing_id:
            raise ProviderError("sailing has no id")
        first = self._json("POST", "/booking-api/api/v1.0/book",
                           self.book_body(sailing_id, sail_date, dur_days, ship_code, guests, None))
        if sink is not None:
            sink["first"] = first
        cabin = (first.get("cabins") or [{}])[0]
        metas = (cabin.get("options") or {}).get("metas") or []
        if not metas:
            raise ProviderError("no room types returned")
        selected = ((cabin.get("selections") or {}).get("meta") or {}).get("code")
        tfpe = self.taxes_from_book(first)
        responses = {selected: first} if selected else {}
        # Cheapest room type first, matching how advisors read a price sheet.
        ordered = sorted(metas, key=lambda m: (m.get("price") is None, m.get("price") or 0))
        todo = [m.get("code") for m in ordered if m.get("code") not in responses and not m.get("isSoldOut")]

        def fetch(mcode):
            try:
                return self._json("POST", "/booking-api/api/v1.0/book",
                                  self.book_body(sailing_id, sail_date, dur_days, ship_code, guests, mcode))
            except ProviderError as exc:
                log.warning("Carnival room type %s failed: %s", mcode, exc)
                return None

        if todo:
            with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
                responses.update(zip(todo, pool.map(fetch, todo)))
        rooms: list[Stateroom] = []
        for meta in ordered:
            rooms.extend(self.parse_book_response(responses.get(meta.get("code")), meta, source, tfpe))
        # Room-type names are usually unique per ship; add the code where they aren't.
        counts: dict[str, int] = {}
        for r in rooms:
            counts[r.name] = counts.get(r.name, 0) + 1
        for r in rooms:
            if counts[r.name] > 1 and r.code:
                r.name = f"{r.name} ({r.code})"
        return rooms

    @staticmethod
    def taxes_from_book(resp: Optional[dict]) -> Optional[float]:
        """Taxes, fees and port expenses per person included in Carnival's shown price:
        the average over guests of cruiseFeesAndExpenses + taxesAndFees."""
        guests = ((resp or {}).get("cabins") or [{}])[0].get("guestPrices") or []
        vals = [
            (g.get("cruiseFeesAndExpenses") or 0) + (g.get("taxesAndFees") or 0)
            for g in guests
            if isinstance(g, dict) and (g.get("cruiseFeesAndExpenses") is not None or g.get("taxesAndFees") is not None)
        ]
        return round(sum(vals) / len(vals), 2) if vals else None

    @staticmethod
    def fare(price: Optional[float], tfpe: Optional[float]) -> Optional[float]:
        """Carnival's all-in price → fare excluding taxes, fees and port expenses."""
        if price is None or price <= 0:
            return None
        if tfpe is None:
            return float(price)
        return round(max(price - tfpe, 0.0), 2)

    @staticmethod
    def parse_book_response(resp: Optional[dict], meta: dict, source: str,
                            tfpe: Optional[float] = None) -> list[Stateroom]:
        mcode = meta.get("code") or ""
        category = META_CATEGORIES.get(mcode, "Other")
        taxes = CarnivalProvider.taxes_from_book(resp) if resp else None
        if taxes is None:
            taxes = tfpe
        types: list[dict] = []
        if resp:
            cabin = (resp.get("cabins") or [{}])[0]
            types = [t for t in ((cabin.get("options") or {}).get("stateroomTypes") or [])
                     if (t.get("metaCode") or mcode) == mcode]
        if not types:  # sold out, or the per-type call failed: keep the room type's "from" price
            price = None if meta.get("isSoldOut") else CarnivalProvider.fare(meta.get("price"), taxes)
            return [Stateroom(
                category=category,
                name=meta.get("name") or mcode,
                code=mcode or None,
                price_per_person=price,
                taxes_fees_per_person=taxes,
                sold_out=price is None,
                notes=((meta.get("rate") or {}).get("name") or None),
                source=source,
            )]
        rooms = []
        for t in types:
            rate = t.get("rate") or {}
            notes = [rate.get("name")] if rate.get("name") else []
            name = t.get("name") or mcode
            if t.get("isGtee"):
                name = f"{name} (guarantee)"
                notes.insert(0, "Carnival assigns the stateroom")
            price = CarnivalProvider.fare(t.get("price"), taxes)
            rooms.append(Stateroom(
                category=category,
                name=name,
                code=t.get("code"),
                price_per_person=price,
                taxes_fees_per_person=taxes,
                sold_out=price is None,
                notes="; ".join(notes) or None,
                source=source,
            ))
        return rooms

    @staticmethod
    def search_rooms(sailing: dict, source: str, tfpe: Optional[float] = None) -> list[Stateroom]:
        """Fallback: the search result's lowest price per room type (all-in unless `tfpe` is known)."""
        out = []
        for key, room in (sailing.get("rooms") or {}).items():
            price = CarnivalProvider.fare(room.get("price"), tfpe) if not room.get("soldOut") else None
            tf = tfpe
            out.append(Stateroom(
                category=SEARCH_ROOM_KEYS.get(key, "Other"),
                name=f"{SEARCH_ROOM_KEYS.get(key, key.title())} (lowest fare)",
                code=room.get("categoryCode") or room.get("metacode"),
                price_per_person=price,
                taxes_fees_per_person=tf,
                sold_out=price is None,
                notes=f"Rate {room['rateCode']}" if room.get("rateCode") else None,
                source=source,
            ))
        return out

    # ── Bulk catalog ─────────────────────────────────────────────────────

    def catalog_ship_codes(self) -> list[str]:
        """Every ship Carnival's search knows (read from its filter options), else our table."""
        try:
            data = self._json("GET", "/cruisesearch/api/search?" + urlencode(
                {"numAdults": 2, "pageNumber": 1, "pageSize": 1, "currency": "USD", "locality": 1}))
            codes = [o["code"] for o in ((data.get("options") or {}).get("shipCode") or []) if o.get("code")]
            if codes:
                return codes
        except ProviderError as exc:
            log.warning("Carnival ship list failed (%s); using the built-in table", exc)
        return sorted(set(SHIP_CODES.values()))

    def ship_itineraries(self, ship_code: str) -> list[dict]:
        """All itineraries (each with all its sailings) for one ship. The unfiltered search
        silently drops ~10% of sailings, so the catalog pages through ship by ship."""
        out, page, last = [], 1, 1
        while page <= last and page <= 50:
            data = self._json("GET", "/cruisesearch/api/search?" + urlencode({
                "numAdults": 2, "pageNumber": page, "pageSize": 100, "shipCode": ship_code,
                "currency": "USD", "locality": 1,
            }))
            results = data.get("results") or {}
            out.extend(results.get("itineraries") or [])
            last = results.get("lastPage") or 1
            page += 1
        return out

    def sailing_taxes(self, itin: dict, sailing: dict) -> Optional[float]:
        """Taxes, fees & port expenses per person for one sailing, from one booking-API call."""
        try:
            resp = self._json("POST", "/booking-api/api/v1.0/book", self.book_body(
                sailing["sailingId"], sailing["departureDate"][:10], itin.get("dur"), itin.get("shipCode"), 2, None))
        except (ProviderError, KeyError, ValueError, TypeError) as exc:
            log.info("Carnival taxes lookup failed for sailing %s: %s", sailing.get("sailingId"), exc)
            return None
        return self.taxes_from_book(resp)

    def iter_catalog(self, taxes: str = "itinerary", workers: int = 4) -> Iterator[CatalogSailing]:
        """Every future Carnival sailing with lead-in fares per room class (2 adults).

        Carnival's search prices include taxes, fees and port expenses and the search
        doesn't say how much they are, so the fare is derived with booking-API calls:
          taxes="itinerary" (default) — one call per itinerary (ship + route), applied to all
                                        its sailings; they vary by ~$1 between dates.
          taxes="sailing"             — one call per sailing (exact, ~6x the calls).
        If a lookup fails the prices for those sailings stay all-in and
        taxes_fees_per_person is None.
        """
        if taxes not in ("itinerary", "sailing"):
            raise ValueError("taxes must be 'itinerary' or 'sailing'")
        ships = self.catalog_ship_codes()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            per_ship = list(pool.map(self.ship_itineraries, ships))

        seen: set[str] = set()
        rows: list[tuple[dict, dict]] = []
        groups: dict[tuple, list[int]] = {}
        today = self._today().isoformat()
        for itins in per_ship:
            for itin in itins:
                gkey = (itin.get("shipCode"), itin.get("code"), itin.get("departurePortCode"), itin.get("dur"))
                for sailing in itin.get("sailings") or []:
                    sid = str(sailing.get("sailingId") or "")
                    if not sid or sid in seen or (sailing.get("departureDate") or "")[:10] < today:
                        continue
                    seen.add(sid)
                    groups.setdefault(gkey if taxes == "itinerary" else (sid,), []).append(len(rows))
                    rows.append((itin, sailing))

        def group_taxes(idx: list[int]) -> Optional[float]:
            # Try up to 3 sailings of the group (the first may be sold out or closed).
            for i in idx[:3]:
                t = self.sailing_taxes(*rows[i])
                if t is not None:
                    return t
            return None

        keys = list(groups)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            tax_by_group = dict(zip(keys, pool.map(lambda k: group_taxes(groups[k]), keys)))
        tax_by_row: dict[int, Optional[float]] = {}
        for k, idx in groups.items():
            for i in idx:
                tax_by_row[i] = tax_by_group[k]

        for i, (itin, sailing) in enumerate(rows):
            yield self.catalog_row(itin, sailing, tax_by_row.get(i))

    def catalog_row(self, itin: dict, sailing: dict, tfpe: Optional[float]) -> CatalogSailing:
        schedule = (itin.get("leadSailing") or {}).get("schedule") or []
        ports = [re.sub(r"[™®]", "", st.get("port") or "").strip()
                 for st in schedule[1:-1] if not is_sea_day(st)]
        prices: dict[str, Optional[float]] = {}
        currency = "USD"
        for key, room in (sailing.get("rooms") or {}).items():
            label = SEARCH_ROOM_KEYS.get(key, key.title())
            prices[label] = None if room.get("soldOut") else self.fare(room.get("price"), tfpe)
            currency = room.get("priceCurrency") or currency
        return CatalogSailing(
            cruise_line=self.cruise_line,
            sailing_key=str(sailing["sailingId"]),
            ship=itin.get("shipName") or itin.get("shipCode") or "",
            sail_date=sailing["departureDate"][:10],
            nights=itin.get("dur"),
            ship_code=itin.get("shipCode"),
            itinerary_name=itin.get("itineraryTitleFormatted") or itin.get("itineraryTitle"),
            departure_port=itin.get("departurePortName"),
            ports=ports,
            booking_url=self.base_url + sailing["sailingURL"] if sailing.get("sailingURL") else None,
            prices=prices,
            taxes_fees_per_person=tfpe,
            currency=currency,
        )

    # ── Add-ons ──────────────────────────────────────────────────────────
    #
    # Carnival's FunShops (/shop/plp/search/<category>) only prices for a sailing
    # once a booking is attached, so without one these are Carnival's published
    # "from" prices. Where the shop can be narrowed we do: spa passes by ship
    # (spaShip facet), Faster to the Fun by embarkation port, excursions by port.
    # Specialty dining has no ship filter, so it is matched against the venues the
    # ship's own page lists. Gratuities and Vacation Protection come from the
    # booking API for this exact sailing.

    def _shop(self, slug: str, q: Optional[str] = None, page: int = 0) -> Optional[dict]:
        path = f"/shop/plp/search/{slug}"
        params = []
        if q or page:  # Carnival ignores `page` without a query
            params.append("q=" + (q or ":recommended"))  # facet query values come pre-encoded
        if page:
            params.append(f"page={page}")
        if params:
            path += "?" + "&".join(params)
        try:
            data = self._json("GET", path, attempts=2)
        except ProviderError as exc:
            log.info("Carnival shop %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _results(data: Optional[dict]) -> list[dict]:
        return ((data or {}).get("productCategorySearchPageData") or {}).get("results") or []

    def _shop_all(self, slug: str, q: Optional[str] = None, max_pages: int = 3) -> Optional[list[dict]]:
        first = self._shop(slug, q)
        if first is None:
            return None
        results = list(self._results(first))
        pages = (((first.get("productCategorySearchPageData") or {}).get("pagination") or {})
                 .get("numberOfPages") or 1)
        for page in range(1, min(pages, max_pages)):
            results.extend(self._results(self._shop(slug, q, page)))
        return results

    def fetch_addons(self, days: list[ItineraryDay], schedule: list[dict], warnings: list[str],
                     ship_code: str = "", ship_name: str = "", nights: Optional[int] = None,
                     embark_code: str = "") -> list[AddOn]:
        """Everything Carnival sells for this sailing. Never raises: failures become warnings."""
        ports = []
        seen = set()
        for i, stop in enumerate(schedule):
            pcode = (stop.get("portCode") or "").upper()
            if i == 0 or i == len(schedule) - 1 or is_sea_day(stop) or pcode in seen:
                continue
            seen.add(pcode)
            ports.append((re.sub(r"[™®]", "", stop.get("port") or "").strip(), pcode))

        jobs = {
            "drink packages": lambda: self._shop_all("drink-packages"),
            "in-room drinks": lambda: self._shop_all("in-room-beverages", max_pages=4),
            "Wi-Fi plans": lambda: self._shop_all("internet-plans"),
            "specialty dining": lambda: self._shop_all("reserve-dining"),
            "ship page": lambda: self.ship_page_text(ship_name),
            "spa passes": lambda: self._shop_all("relaxation-areas", f":price-asc:spaShip:{ship_code}"),
            "photo packages": lambda: self._shop_all("dream-studio"),
            "arcade": lambda: self._shop_all("entertainment-packages"),
            "Faster to the Fun": lambda: (self._shop_all("faster-to-the-fun", f":recommended:category:{embark_code}")
                                          if embark_code else []),
            "port list": lambda: self.excursion_port_facets(),
        }
        out: dict[str, object] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {k: pool.submit(fn) for k, fn in jobs.items()}
            for k, fut in futures.items():
                try:
                    out[k] = fut.result()
                except Exception as exc:  # an add-on must never fail research
                    log.warning("Carnival add-on %s failed: %s", k, exc)
                    out[k] = None

            facets = out.get("port list") or []
            port_futs = [(name, code, pool.submit(self.fetch_port_excursions, name, code, facets))
                         for name, code in ports]
            excursions: list[AddOn] = []
            missing_ports = []
            for name, code, fut in port_futs:
                try:
                    found = fut.result()
                except Exception as exc:
                    log.warning("Carnival excursions for %s failed: %s", name, exc)
                    found = None
                if found is None:
                    missing_ports.append(name)
                else:
                    excursions.extend(found)

        failed = [k for k in ("drink packages", "Wi-Fi plans", "specialty dining", "spa passes")
                  if out.get(k) is None]
        addons: list[AddOn] = []
        addons += self.map_products(out.get("drink packages") or [], "beverage")
        addons += self.map_products(out.get("in-room drinks") or [], "beverage", default_unit="flat",
                                    default_label="each, delivered to the stateroom")
        addons += self.map_products(out.get("Wi-Fi plans") or [], "internet")
        ship_text = out.get("ship page")
        dining = self.filter_dining(out.get("specialty dining") or [], ship_code, ship_text)
        addons += self.map_products(dining, "dining", default_unit="per_person", default_label="per person")
        if out.get("specialty dining") and not ship_text:
            warnings.append("Couldn't read the ship's page, so the specialty dining list is Carnival's whole "
                            "fleet (deduplicated) — check which venues this ship has.")
        addons += self.map_products(self.pick_spa_passes(out.get("spa passes") or [], nights), "activity",
                                    default_unit="per_person", default_label="per guest")
        addons += self.map_products(out.get("photo packages") or [], "photo", default_unit="flat",
                                    default_label="per package")
        addons += self.map_products(out.get("arcade") or [], "activity", default_unit="flat",
                                    default_label="per package")
        addons += self.map_products(out.get("Faster to the Fun") or [], "other", default_unit="flat",
                                    default_label="per stateroom")
        addons += excursions
        if failed:
            warnings.append(f"Couldn't load Carnival add-ons for: {', '.join(failed)}.")
        if missing_ports:
            warnings.append(f"No Carnival shore excursions found online for: {', '.join(missing_ports)}.")
        return addons

    # Ship page → which specialty restaurants this ship has.

    def ship_page_text(self, ship_name: str) -> Optional[str]:
        if not ship_name:
            return None
        slug = re.sub(r"[^a-z0-9]+", "-", ship_name.lower()).strip("-")
        url = f"{self.base_url}/cruise-ships/{slug}"
        if self._blocked:  # carnival.com refuses this server: render the page in Cloudflare's browser
            try:
                page = self.cf.content({"url": url, "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000}})
            except (BrowserRenderingError, AttributeError):
                return None
        else:
            try:
                resp = self.http.get(url, headers={"user-agent": USER_AGENT})
                with self._lock:
                    self.http_calls += 1
                if resp.status_code != 200:
                    return None
                page = resp.text
            except httpx.HTTPError:
                return None
        soup = BeautifulSoup(page, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).replace("’", "'").replace("&#39;", "'").lower()
        return text if len(text) > 200 else None

    @staticmethod
    def dining_key(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", name.lower().replace("’", "'").replace("'", "")).strip()

    @staticmethod
    def filter_dining(results: list[dict], ship_code: str, ship_text: Optional[str]) -> list[dict]:
        """Deduplicate Carnival's fleet-wide dining list and keep what this ship has."""
        by_key: dict[str, dict] = {}
        for r in results:
            if (r.get("price") or {}).get("value") is None:
                continue
            key = CarnivalProvider.dining_key(r.get("name") or "")
            if key and (key not in by_key or r["price"]["value"] < by_key[key]["price"]["value"]):
                by_key[key] = r
        if not ship_text:
            return list(by_key.values())
        out = []
        for key, r in by_key.items():
            needs = None
            for pattern, keywords in DINING_VENUES:
                if re.search(pattern, key):
                    needs = keywords
                    break
            if needs is None:  # unknown venue: keep it only if the ship's page names it
                needs = [key]
            if any(k in ship_text for k in needs) or (needs is CARNIVAL_KITCHEN and ship_code in EXCEL_SHIPS):
                out.append(r)
        return out

    @staticmethod
    def pick_spa_passes(results: list[dict], nights: Optional[int]) -> list[dict]:
        """Thermal-suite passes for this ship: the one for this cruise length plus day/couples passes."""
        def days(name: str):
            m = re.search(r"(\d+)\s*-\s*(\d+)\s*-?\s*day", name, re.I)
            if m:
                return int(m.group(1)), int(m.group(2))
            m = re.search(r"(\d+)\s*-?\s*day", name, re.I)
            return (int(m.group(1)), int(m.group(1))) if m else None

        cruise = [r for r in results if "cruise" in (r.get("name") or "").lower() and days(r.get("name") or "")]
        other = [r for r in results if r not in cruise]
        if nights:
            match = [r for r in cruise if days(r["name"])[0] <= nights <= days(r["name"])[1]]
            if match:
                return match + other
        return results

    # Shore excursions: Carnival's shore-excursion catalogue lists every port it sells
    # tours in ("Ports & Destination" facet); pick this port's entry, confirm by port code.

    def excursion_port_facets(self) -> list[tuple[str, str]]:
        data = self._shop("shoreex")
        facets = ((data or {}).get("productCategorySearchPageData") or {}).get("facets") or []
        for f in facets:
            if f.get("name") == "Ports & Destination":
                return [(v.get("name") or "", ((v.get("query") or {}).get("query") or {}).get("value") or "")
                        for v in f.get("values") or []]
        return []

    @staticmethod
    def _tokens(name: str) -> set[str]:
        norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        norm = re.sub(r"[™®]", "", norm)
        return {t for t in re.split(r"[^a-z0-9]+", norm) if t and t not in {"the", "of", "de", "del"}}

    @staticmethod
    def rank_port_facets(port_name: str, facets: list[tuple[str, str]]) -> list[str]:
        """Facet queries most likely to be `port_name`, best first."""
        want = CarnivalProvider._tokens(port_name)
        first = CarnivalProvider._tokens(port_name.split(",")[0].split("(")[0])
        scored = []
        for name, query in facets:
            have = CarnivalProvider._tokens(name)
            if not query or not (first & have):
                continue
            score = len(want & have) / len(want | have) + (1 if first <= have else 0)
            scored.append((score, query))
        return [q for _, q in sorted(scored, key=lambda x: -x[0])[:3]]

    def fetch_port_excursions(self, port_name: str, port_code: str,
                              facets: Optional[list[tuple[str, str]]] = None) -> Optional[list[AddOn]]:
        def ok(results):
            codes = {((r.get("shoreExData") or {}).get("portData") or {}).get("code") for r in results}
            return bool(results) and port_code in codes

        for query in self.rank_port_facets(port_name, facets or []):
            results = self._shop_all("shoreex", query)
            if results and ok(results):
                return self.map_excursions(
                    [r for r in results if ((r.get("shoreExData") or {}).get("portData") or {}).get("code")
                     in (port_code, None)], port_name)
        for slug in port_slugs(port_name):  # fallback: the port's own category slug
            data = self._shop(slug)
            results = self._results(data)
            if results and ((data or {}).get("categoryName") == port_code or ok(results)):
                return self.map_excursions(results, port_name)
        return None

    # Booking-API extras for this exact sailing.

    @staticmethod
    def booking_addons(resp: Optional[dict], nights: Optional[int], source: str) -> list[AddOn]:
        guests = ((resp or {}).get("cabins") or [{}])[0].get("guestPrices") or []
        g = guests[0] if guests and isinstance(guests[0], dict) else {}
        out = []
        if g.get("gratuity"):
            total = float(g["gratuity"])
            per_day = round(total / nights, 2) if nights else None
            out.append(AddOn(
                kind="other",
                name="Gratuities (prepaid)",
                price=per_day if per_day is not None else total,
                price_unit="per_person_per_day" if per_day is not None else "per_person",
                unit_label="per person, per day" if per_day is not None else "per person, per cruise",
                port=None,
                description=f"${total:,.2f} per person for this {nights}-night sailing, from Carnival's booking "
                            "system (standard staterooms; suites are charged more). Added to onboard accounts "
                            "if not prepaid.",
                source=source,
            ))
        if g.get("insurance"):
            out.append(AddOn(
                kind="other",
                name="Vacation Protection",
                price=float(g["insurance"]),
                price_unit="per_person",
                unit_label="per person",
                port=None,
                description="Carnival's Vacation Protection plan as quoted by its booking system for this sailing "
                            "(varies with the fare).",
                source=source,
            ))
        return out

    @staticmethod
    def fee_percent(r: dict) -> float:
        f = r.get("feesData") or {}
        pcts = []
        if f.get("subjectToGratuity", True) and f.get("gratuityAmount"):
            pcts.append(float(f["gratuityAmount"]))
        if f.get("subjectToServiceFee", True) and f.get("serviceFee"):
            pcts.append(float(f["serviceFee"]))
        return max(pcts) if pcts else 0.0

    @staticmethod
    def map_products(results: list[dict], kind: str, default_unit: str = "flat",
                     default_label: Optional[str] = None) -> list[AddOn]:
        out = []
        seen = set()
        for r in results:
            base = (r.get("price") or {}).get("value")
            if base is None or r.get("code") in seen:
                continue
            seen.add(r.get("code"))
            fs = r.get("funShopData") or {}
            label = fs.get("salesPerUnitDescription") or None
            if label is None:
                unit, label = default_unit, default_label
            elif "up to" in label.lower() and "device" in label.lower():
                unit = "per_device_per_day"  # e.g. multi-device plan, priced per day for up to 4 devices
            else:
                unit = unit_from_label(label)
            notes = []
            summary = _strip_html(r.get("summary"))
            if summary and summary.lower() != (r.get("name") or "").lower():
                notes.append(summary[:200])
            if kind == "beverage" and fs.get("childSalesPerUnitDescription"):
                notes.append(f"Adult price; child pricing also available ({fs['childSalesPerUnitDescription']}).")
            pct = CarnivalProvider.fee_percent(r)
            price = float(base)
            if pct:
                price = round(base * (1 + pct / 100), 2)
                notes.append(f"Price includes Carnival's {pct:g}% service charge (${base:,.2f} before it).")
            notes.append("Carnival's published 'from' price; the exact price shows once booked.")
            out.append(AddOn(
                kind=kind,
                name=(r.get("name") or r.get("code") or "").strip(),
                price=price,
                price_unit=unit,
                unit_label=label,
                port=None,
                description=" ".join(notes),
                source=BASE_URL + (r.get("url") or "/shop"),
            ))
        return out

    @staticmethod
    def map_excursions(results: list[dict], port_name: str) -> list[AddOn]:
        out = []
        for r in results:
            sx = r.get("shoreExData") or {}
            price = (r.get("price") or sx.get("basePriceData") or {}).get("value")
            if price is None:
                continue
            bits = []
            if sx.get("durationAmount"):
                unit = {"H": "hours", "M": "minutes", "D": "days"}.get(sx.get("durationType"), "")
                bits.append(f"{sx['durationAmount']:g} {unit}".strip())
            child = (sx.get("childBasePriceData") or {}).get("value")
            if child is not None:
                bits.append(f"child from ${child:,.2f}")
            level = (sx.get("activityLevel") or {}).get("name")
            if level:
                bits.append(f"activity: {level}")
            out.append(AddOn(
                kind="excursion",
                name=r.get("name") or "",
                price=float(price),
                price_unit="per_person",
                unit_label="per adult",
                port=port_name,
                description="; ".join(bits) or None,
                source=BASE_URL + (r.get("url") or "/shore-excursions"),
            ))
        return out
