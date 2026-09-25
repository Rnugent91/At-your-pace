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
SHOP_CATEGORIES = [("drink-packages", "beverage"), ("internet-plans", "internet")]

MAX_EXCURSIONS_PER_PORT = 24  # one page of Carnival's results


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
        try:
            research.staterooms = self.fetch_staterooms(
                sailing.get("sailingId"), req.sail_date, itin.get("dur"), code, guests, booking_url or self.base_url
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

        research.addons = self.fetch_addons(research.itinerary, schedule, warnings)
        if research.addons:
            research.sources.append(f"{self.base_url}/shop (FunShops)")
            warnings.append(
                "Carnival add-on prices are published 'from' prices, not quoted for this sailing; "
                "drink packages exclude Carnival's service charge."
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

    def fetch_staterooms(self, sailing_id, sail_date, dur_days, ship_code, guests, source) -> list[Stateroom]:
        if not sailing_id:
            raise ProviderError("sailing has no id")
        first = self._json("POST", "/booking-api/api/v1.0/book",
                           self.book_body(sailing_id, sail_date, dur_days, ship_code, guests, None))
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

    def _shop(self, slug: str) -> Optional[dict]:
        try:
            data = self._json("GET", f"/shop/plp/search/{slug}")
        except ProviderError as exc:
            log.info("Carnival shop %s: %s", slug, exc)
            return None
        return data if isinstance(data, dict) else None

    def fetch_addons(self, days: list[ItineraryDay], schedule: list[dict], warnings: list[str]) -> list[AddOn]:
        addons: list[AddOn] = []
        failed = []
        for slug, kind in SHOP_CATEGORIES:
            data = self._shop(slug)
            if data is None:
                failed.append(slug.replace("-", " "))
                continue
            results = (data.get("productCategorySearchPageData") or {}).get("results") or []
            addons.extend(self.map_products(results, kind))

        # Shore excursions: one FunShops category per port (skip embark/debark and sea days).
        missing_ports = []
        seen = set()
        for i, stop in enumerate(schedule):
            if i == 0 or i == len(schedule) - 1 or is_sea_day(stop):
                continue
            pcode = (stop.get("portCode") or "").upper()
            if pcode in seen:
                continue
            seen.add(pcode)
            port_name = re.sub(r"[™®]", "", stop.get("port") or "").strip()
            found = self.fetch_port_excursions(port_name, pcode)
            if found is None:
                missing_ports.append(port_name)
            else:
                addons.extend(found)
        if failed:
            warnings.append(f"Couldn't load Carnival add-ons for: {', '.join(failed)}.")
        if missing_ports:
            warnings.append(f"No Carnival shore excursions found online for: {', '.join(missing_ports)}.")
        return addons

    def fetch_port_excursions(self, port_name: str, port_code: str) -> Optional[list[AddOn]]:
        for slug in port_slugs(port_name):
            data = self._shop(slug)
            if not data:
                continue
            results = (data.get("productCategorySearchPageData") or {}).get("results") or []
            # Only trust a slug whose results really are this port's.
            codes = {((r.get("shoreExData") or {}).get("portData") or {}).get("code") for r in results}
            if results and (data.get("categoryName") == port_code or port_code in codes):
                return self.map_excursions(results[:MAX_EXCURSIONS_PER_PORT], port_name)
        return None

    @staticmethod
    def map_products(results: list[dict], kind: str) -> list[AddOn]:
        out = []
        for r in results:
            price = (r.get("price") or {}).get("value")
            if price is None:
                continue
            fs = r.get("funShopData") or {}
            label = fs.get("salesPerUnitDescription") or None
            if label is None:
                unit = "flat"
            elif "up to" in label.lower() and "device" in label.lower():
                unit = "per_device_per_day"  # e.g. multi-device plan, priced per day for up to 4 devices
            else:
                unit = unit_from_label(label)
            desc = _strip_html(r.get("summary")) or None
            if desc and desc.lower() == (r.get("name") or "").lower():
                desc = None
            if kind == "beverage" and fs.get("childSalesPerUnitDescription"):
                desc = f"Adult price; child pricing also available ({fs['childSalesPerUnitDescription']})."
            if kind == "beverage":
                desc = ((desc + " ") if desc else "") + "Before Carnival's service charge (typically 20%)."
            out.append(AddOn(
                kind=kind,
                name=r.get("name") or r.get("code") or "",
                price=float(price),
                price_unit=unit,
                unit_label=label,
                port=None,
                description=desc,
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
