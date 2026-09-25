"""Norwegian Cruise Line: live prices from ncl.com's own JSON APIs.

Endpoints (the ones ncl.com's "vacations" search and "vacation builder" pages call,
found by capturing the site's XHR traffic in Sept 2026):
  * search      GET  /api/v2/vacations/search?ships=GETAWAY&dates=May-2027&limit=..&offset=..
                     → itineraries (code, ship, ports, lead price). Without filters and with
                       limit=1000 it lists every itinerary in one call (~820, incl. cruisetours).
  * sailings    GET  /api/vacations/sailings/{itineraryCode}?guests=2
                     → every sailing (packageId, sailId, dates) × room type (INSIDE, OCEANVIEW,
                       BALCONY, MINISUITE, SUITE, HAVEN, STUDIO) with its lowest price.
  * day-by-day  GET  /api/vacation-builder/itinerary/{itineraryCode}/package/{packageId}/events
  * categories  POST /api/vacation-builder/v2/stateroom-types-availability
                     {"sailingFilters":[{"packageId","numberOfGuests","filterId"}]}
                     → every stateroom category (IA, IB, OB, BA, B1, M1, H2, ...) with its price.
  * price-summary POST /api/vacation-builder/price-summary
                     {"packageId", "stateroomFilters":[{stateroomTypeCode, pricedCategoryCode,
                      fareCodes, numberOfGuests, ...}]}
                     → the checkout breakdown: fare, Free at Sea charges, taxes & port fees.
                       (Read-only: it does not hold a cabin — the site's /cabin/manage-cabin
                       call does, and this provider never makes it.)

Prices on ncl.com are ALL-IN: every price the search, sailings and categories calls
return is cruise fare + government taxes, fees & port expenses, per person, for the
number of guests asked for. The fare is recovered by subtracting the taxes from one
price-summary call per sailing (taxes are the same for every category of a sailing;
checked live on several categories and ships).

Free at Sea (NCL's bundle — formerly "More at Sea"): open bar, specialty dining, Wi-Fi
and excursion credit. It does NOT change the fare; choosing it adds the package
gratuities/fees shown by price-summary (e.g. $116 pp on a 3-night sailing). The quoted
fares are therefore the same with or without it; its cost is returned as add-ons and
noted on each stateroom.

Room types → our category: INSIDE → Interior, OCEANVIEW → Ocean View, BALCONY → Balcony,
MINISUITE (Club Balcony Suite), SUITE and HAVEN (The Haven) → Suite, STUDIO → Other.

All of these answer plain server requests with browser-like headers today. If Akamai
starts refusing a server IP (401/403 or network errors), the same request is replayed
from inside a real ncl.com page through Cloudflare Browser Rendering (when configured).
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest

log = logging.getLogger(__name__)

BASE_URL = "https://www.ncl.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "user-agent": BROWSER_UA,
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": BASE_URL,
    "referer": BASE_URL + "/vacations",
}

# Ship name → NCL ship code, as listed by the search API's "ships" filter (Sept 2026).
# The lookup checks the ship name NCL returns, so a wrong entry fails loudly. Advisors
# can also type the code itself (e.g. "GETAWAY", "PRIDE_AMER").
SHIP_CODES = {
    "norwegian aqua": "AQUA",
    "norwegian aura": "AURA",
    "norwegian bliss": "BLISS",
    "norwegian breakaway": "BREAKAWAY",
    "norwegian dawn": "DAWN",
    "norwegian encore": "ENCORE",
    "norwegian epic": "EPIC",
    "norwegian escape": "ESCAPE",
    "norwegian gem": "GEM",
    "norwegian getaway": "GETAWAY",
    "norwegian jade": "JADE",
    "norwegian jewel": "JEWEL",
    "norwegian joy": "JOY",
    "norwegian luna": "LUNA",
    "norwegian pearl": "PEARL",
    "norwegian prima": "PRIMA",
    "norwegian spirit": "SPIRIT",
    "norwegian star": "STAR",
    "norwegian sun": "SUN",
    "norwegian viva": "VIVA",
    "pride of america": "PRIDE_AMER",
}

# Room type (stateroomType code) → our category, in the order a price sheet reads.
TYPE_CATEGORY = {
    "STUDIO": "Other",
    "INSIDE": "Interior",
    "OCEANVIEW": "Ocean View",
    "BALCONY": "Balcony",
    "MINISUITE": "Suite",
    "SUITE": "Suite",
    "HAVEN": "Suite",
}
TYPE_ORDER = {code: i for i, code in enumerate(TYPE_CATEGORY)}

# Catalog class labels. Club Balcony Suite (NCL's mini-suite) and The Haven are kept as
# their own classes; "Suite" is the cheapest full suite (SUITE or HAVEN, computed in
# catalog_rows). Studios are
# solo-only, so they never have a 2-guest price and are left out.
CATALOG_CLASSES = {
    "INSIDE": "Interior",
    "OCEANVIEW": "Ocean View",
    "BALCONY": "Balcony",
    "MINISUITE": "Club Balcony Suite",
    "HAVEN": "The Haven",
}

# (room type, category) tried in order for the per-sailing taxes lookup in the catalog.
# Taxes don't depend on the category; IX (guarantee inside) prices on every ship.
TAX_PROBES = [("INSIDE", "IX"), ("INSIDE", "IA"), ("OCEANVIEW", "OX"), ("BALCONY", "BX")]

ADDON_KINDS = {
    "beverage-package-offer": "beverage",
    "dining-offer": "dining",
    "wifi-package-offer": "internet",
    "shorex-offer": "excursion",
}

CATALOG_WORKERS = 8
RETRIES = 4


def ship_code_for(ship: str) -> Optional[str]:
    s = re.sub(r"\s+", " ", (ship or "").strip().lower()).removeprefix("ncl ").strip()
    if not s:
        return None
    if s in SHIP_CODES:
        return SHIP_CODES[s]
    if f"norwegian {s}" in SHIP_CODES:
        return SHIP_CODES[f"norwegian {s}"]
    code = s.upper().replace(" ", "_")
    if code in SHIP_CODES.values():
        return code
    return None


def parse_booking_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """(itineraryCode, packageId) from an ncl.com vacation-builder / search URL."""
    if not url or "ncl.com" not in url.lower():
        return None, None
    q = parse_qs(urlparse(url.strip()).query)
    code = (q.get("itineraryCode") or [None])[0]
    pkg = (q.get("packageId") or [None])[0]
    if not code:
        m = re.search(r"[-/]([A-Z_]+\d+[A-Z]{6,})(?:[/?#]|$)", url)
        code = m.group(1) if m else None
    return (code or None), (pkg or None)


def to_24h(hh: str, mm: str, ampm: str) -> str:
    h = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
    return f"{h:02d}:{mm}"


def parse_schedule(text: Optional[str], embark: bool = False, debark: bool = False) -> tuple[Optional[str], Optional[str]]:
    """'7:00 AM - 5:00 PM' → ('07:00', '17:00'); embark '4:00 PM' → (None, '16:00')."""
    times = [to_24h(*m) for m in re.findall(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])", text or "")]
    if not times:
        return None, None
    if len(times) >= 2:
        return times[0], times[-1]
    low = (text or "").lower()
    if embark or "depart" in low:
        return None, times[0]
    return times[0], None  # disembarkation, or arrival on the first day of an overnight


def ordered_ports(sequence: Optional[str], ports_of_call: list[dict]) -> list[str]:
    """Ports of call in visiting order, read from a sailing's event sequence.

    'MIAATSEAPOP2STT2TOV2ATSEANPI2MIA' + ports of call {POP, STT, TOV, NPI} →
    [Puerto Plata, St. Thomas, Tortola, Great Stirrup Cay]. Codes aren't fixed-width and
    scenic stops (e.g. 'GB' Glacier Bay) appear too, so only known port codes are matched.
    Falls back to the itinerary's own list if the sequence doesn't account for every port.
    """
    titles = {p.get("code"): p.get("title") for p in ports_of_call if p.get("code") and p.get("title")}
    fallback = [p.get("title") for p in ports_of_call if p.get("title")]
    text = sequence or ""  # digits are separators, but some codes contain them (HK1, BE9)
    codes = sorted(titles, key=len, reverse=True)
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("ATSEA", i):
            i += 5
            continue
        hit = next((c for c in codes if text.startswith(c, i)), None)
        if hit:
            if not out or out[-1] != titles[hit]:
                out.append(titles[hit])
            i += len(hit)
        else:
            i += 1
    return out if set(out) == set(titles.values()) else fallback


def _nights(start: Optional[str], end: Optional[str]) -> Optional[int]:
    try:
        return (date.fromisoformat((end or "")[:10]) - date.fromisoformat((start or "")[:10])).days
    except ValueError:
        return None


def _money(v: Any) -> Optional[float]:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


class NorwegianProvider:
    name = "Norwegian (live)"
    cruise_line = "Norwegian"

    def __init__(self, cf: CloudflareBrowser, http: Optional[httpx.Client] = None, base_url: str = BASE_URL,
                 catalog_taxes: bool = True):
        self.cf = cf
        self.http = http or httpx.Client(timeout=45, follow_redirects=True,
                                         limits=httpx.Limits(max_connections=16))
        self.base_url = base_url.rstrip("/")
        # One price-summary call per sailing splits NCL's all-in price into fare + taxes.
        self.catalog_taxes = catalog_taxes
        self._blocked = False  # set once ncl.com refuses server requests; later calls go via CF
        self._lock = threading.Lock()
        self.calls = 0  # HTTP calls made (for the catalog report)

    def handles(self, cruise_line: str) -> bool:
        s = cruise_line.lower()
        return "norwegian" in s or re.search(r"\bncl\b", s) is not None

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _json(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None):
        """Call an ncl.com JSON endpoint directly (with retries), or from inside a browser if blocked."""
        if params:
            path = f"{path}?{urlencode(params)}"
        last = ""
        if not self._blocked:
            for attempt in range(RETRIES):
                with self._lock:
                    self.calls += 1
                try:
                    resp = self.http.request(method, self.base_url + path, json=body, headers=BROWSER_HEADERS)
                except httpx.HTTPError as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    self._sleep(1 + attempt)
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("retry-after", "")
                    self._sleep(float(retry_after) if retry_after.isdigit() else 2 * (attempt + 1))
                    continue
                if resp.status_code in (401, 403):
                    last = f"HTTP {resp.status_code}"
                    break
                if resp.status_code >= 400:
                    raise ProviderError(f"NCL {path.split('?')[0]} → HTTP {resp.status_code}: {resp.text[:160]}")
                try:
                    return resp.json()
                except ValueError as exc:
                    last = f"non-JSON reply: {resp.text[:80]!r}"
                    if "<html" not in resp.text[:500].lower():
                        raise ProviderError(f"NCL {path.split('?')[0]} returned {last}") from exc
                    break  # a bot-challenge page instead of JSON
            if self.cf is None or not self.cf.configured:
                raise ProviderError(f"NCL request {path.split('?')[0]} failed: {last}")
            log.warning("ncl.com direct call %s failed (%s); switching to Cloudflare browser", path, last)
            self._blocked = True
        return self._json_via_browser(method, path, body)

    def _json_via_browser(self, method: str, path: str, body: Optional[dict]):
        if self.cf is None or not self.cf.configured:
            raise ProviderError("ncl.com blocked the request. Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN.")
        opts: dict = {"method": method, "headers": {"accept": "application/json", "content-type": "application/json"},
                      "credentials": "include"}
        if body is not None:
            opts["body"] = json.dumps(body)
        script = (
            "(async function(){var out;try{var r=await fetch(" + json.dumps(path) + "," + json.dumps(opts) + ");"
            "out=r.status>=400?'ERR:HTTP '+r.status+' '+(await r.text()).slice(0,200):await r.text();}"
            "catch(e){out='ERR:'+(e&&e.message||e);}"
            "var d=document.createElement('div');d.id='qp-ncl';"
            "d.setAttribute('data-json',encodeURIComponent(out));document.body.appendChild(d);})();"
        )
        with self._lock:
            self.calls += 1
        try:
            html = self.cf.content({
                "url": self.base_url + "/vacations",
                "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                "addScriptTag": [{"content": script}],
                "waitForSelector": {"selector": "#qp-ncl", "timeout": 55000},
            })
        except BrowserRenderingError as exc:
            raise ProviderError(f"NCL request {path.split('?')[0]} failed in browser: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="qp-ncl")
        if node is None:
            raise ProviderError(f"NCL request {path.split('?')[0]} returned no data in browser")
        text = unquote(node.get("data-json", ""))
        if text.startswith("ERR:"):
            raise ProviderError(f"NCL request {path.split('?')[0]} failed in-page: {text[4:200]}")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ProviderError(f"NCL request {path.split('?')[0]} returned non-JSON in-page: {text[:120]}") from exc

    # ── API wrappers ─────────────────────────────────────────────────────

    def search(self, **filters) -> list[dict]:
        """All itineraries matching the filters (paginated)."""
        out: list[dict] = []
        offset, limit = 0, int(filters.pop("limit", 500))
        while True:
            data = self._json("GET", "/api/v2/vacations/search", {**filters, "limit": limit, "offset": offset})
            items = data.get("itineraries") or []
            out += items
            total = data.get("total") or 0
            offset += limit
            if not items or offset >= total:
                return out

    def sailings(self, itinerary_code: str, guests: int = 2) -> dict:
        return self._json("GET", f"/api/vacations/sailings/{itinerary_code}", {"guests": guests})

    def events(self, itinerary_code: str, package_id: str) -> list[dict]:
        data = self._json("GET", f"/api/vacation-builder/itinerary/{itinerary_code}/package/{package_id}/events")
        return data if isinstance(data, list) else []

    def availability(self, package_id: str, guests: int) -> dict:
        data = self._json("POST", "/api/vacation-builder/v2/stateroom-types-availability",
                          body={"sailingFilters": [{"packageId": package_id, "numberOfGuests": guests, "filterId": "0"}]})
        res = ((data.get("results") or [{}])[0]) if isinstance(data, dict) else {}
        if not res.get("result"):
            err = ((res.get("error") or {}).get("error") or {}).get("message") or "no result"
            raise ProviderError(f"NCL stateroom availability: {err}")
        return res["result"]

    def price_summary(self, package_id: str, type_code: str, category: str, guests: int,
                      fare_codes: Optional[list[str]] = None) -> dict:
        body = {
            "packageId": package_id,
            "stateroomFilters": [{
                "id": "0", "mainCabin": True, "numberOfGuests": guests, "stateroomTypeCode": type_code,
                "pricedCategoryCode": category, "fareCodes": fare_codes or [], "guests": [], "vouchers": [],
            }],
            "userFareCodes": [],
        }
        return self._json("POST", "/api/vacation-builder/price-summary", body=body)

    # ── Entry point ──────────────────────────────────────────────────────

    def booking_url(self, itinerary_code: str, package_id: str, guests: int = 2) -> str:
        return (f"{self.base_url}/vacation-builder/planning/stateroom?"
                + urlencode({"itineraryCode": itinerary_code, "packageId": package_id, "guests": guests}))

    def research(self, req: ResearchRequest) -> SailingResearch:
        guests = max(1, req.adults + req.children)
        url_code, url_pkg = parse_booking_url(req.booking_url)
        ship_code = ship_code_for(req.ship)
        if not ship_code and not url_code:
            raise ProviderError(f"Unknown Norwegian ship '{req.ship}' — enter the ship name, e.g. Norwegian Getaway")

        found = self.find_sailing(ship_code, req.sail_date, guests, url_code, url_pkg)
        if not found:
            raise ProviderError(f"No Norwegian sailing found for {req.ship} on {req.sail_date}")
        code, rows, details = found
        first = rows[0]
        pkg = first["packageId"]
        ship = details.get("ship") or {}
        if ship_code and ship.get("code") and ship["code"] != ship_code:
            raise ProviderError(f"NCL itinerary {code} is on {ship.get('title')}, not {req.ship}")

        source = self.booking_url(code, pkg, guests)
        research = SailingResearch(
            cruise_line="Norwegian",
            ship=ship.get("title") or req.ship,
            sail_date=first["sailStartDate"][:10],
            nights=_nights(first.get("sailStartDate"), first.get("sailEndDate")),
            departure_port=(details.get("embarkationPort") or {}).get("title"),
            itinerary_name=details.get("title"),
            itinerary=[],
            currency=first.get("currencyCode") or "USD",
            staterooms=[],
            addons=[],
            sources=[source],
            warnings=[],
        )
        warnings = research.warnings
        if first.get("isPackage") or first.get("isFlyCruise"):
            warnings.append("This NCL price is for a package (cruisetour or fly-cruise), not cruise-only.")

        try:
            research.itinerary = self.map_days(self.events(code, pkg), research.sail_date)
        except ProviderError as exc:
            log.warning("NCL itinerary fetch failed: %s", exc)
            warnings.append(f"NCL's day-by-day itinerary couldn't be loaded: {exc}")

        taxes: Optional[float] = None
        fas: dict[str, dict] = {}
        try:
            avail = self.availability(pkg, guests)
            fas, taxes = self.fetch_type_summaries(pkg, avail, guests)
            research.staterooms = self.map_categories(avail, taxes, fas, guests, source)
        except ProviderError as exc:
            log.warning("NCL stateroom categories failed: %s", exc)
            warnings.append(f"NCL's stateroom categories couldn't be loaded ({exc}); showing lowest price per room type.")
        if not research.staterooms:
            if taxes is None:
                taxes = self.sailing_taxes(pkg, guests)
            research.staterooms = self.lead_in_rooms(rows, taxes, source)
        if taxes is None:
            warnings.append("NCL prices include taxes, fees & port expenses; the split couldn't be loaded, "
                            "so the prices shown are all-in.")

        research.addons = self.fas_addons(fas)
        if research.addons:
            summary = next(iter(fas.values()))
            warnings.append(
                f"NCL fares are the same with or without Free at Sea; taking it adds "
                f"${summary['fas_pp']:,.2f} per person in package gratuities/fees (see add-ons)."
            )
        if guests != 2:
            warnings.append(f"NCL prices are the average per person for {guests} guests in the stateroom.")
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def find_sailing(self, ship_code: Optional[str], sail_date: str, guests: int,
                     url_code: Optional[str] = None, url_pkg: Optional[str] = None
                     ) -> Optional[tuple[str, list[dict], dict]]:
        """(itinerary code, the sailing's per-room-type rows, itinerary details)."""
        codes: list[str] = []
        if url_code:
            codes.append(url_code)
        if ship_code:
            try:
                month = date.fromisoformat(sail_date).strftime("%b-%Y")
            except ValueError as exc:
                raise ProviderError(f"Bad sail date '{sail_date}'") from exc
            items = self.search(ships=ship_code, dates=month, limit=100)
            # Cruise-only itineraries before cruisetours (same ship, same dates, land package added).
            items.sort(key=lambda i: (i.get("bundleType") != "cruise", i.get("code") or ""))
            codes += [i["code"] for i in items if i.get("code") and i["code"] not in codes]
        errors = []
        for code in codes:
            try:
                data = self.sailings(code, guests)
            except ProviderError as exc:
                errors.append(exc)
                continue
            rows = self.pick_sailing(data.get("pricingStateRooms") or [], sail_date, url_pkg if code == url_code else None)
            if rows:
                return code, rows, data.get("itineraryDetails") or {}
        if errors and len(errors) == len(codes):
            raise errors[-1]
        return None

    @staticmethod
    def pick_sailing(rows: list[dict], sail_date: str, package_id: Optional[str] = None) -> list[dict]:
        if package_id:
            hit = [r for r in rows if str(r.get("packageId")) == str(package_id)]
            if hit:
                return hit
        pkgs = [r.get("packageId") for r in rows if (r.get("sailStartDate") or "")[:10] == sail_date]
        return [r for r in rows if pkgs and r.get("packageId") == pkgs[0]]

    # ── Itinerary ────────────────────────────────────────────────────────

    @staticmethod
    def map_days(events: list[dict], sail_date: str) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        out = []
        for day in sorted(events, key=lambda d: d.get("day") or 0):
            n = day.get("day") or len(out) + 1
            evs = sorted(day.get("events") or [], key=lambda e: e.get("order") or 0)
            ports = [e for e in evs if e.get("eventType") == "PORT"]
            when = next((e.get("date") for e in evs if e.get("date")), None)
            iso = when[:10] if when else start.fromordinal(start.toordinal() + n - 1).isoformat()
            if ports:
                arrive = depart = None
                for i, e in enumerate(ports):
                    pi = e.get("portInfo") or {}
                    a, d = parse_schedule(pi.get("dailySchedule"), pi.get("isEmbarkation"), pi.get("isDisembarkation"))
                    arrive = arrive if i else a
                    depart = d if i == len(ports) - 1 else depart
                out.append(ItineraryDay(day=n, date=iso, port=" / ".join(e.get("title") or "Port" for e in ports),
                                        arrive=arrive, depart=depart))
            else:
                scenic = next((e.get("title") for e in evs if e.get("eventType") == "SCENIC" and e.get("title")), None)
                out.append(ItineraryDay(day=n, date=iso, port=f"At Sea ({scenic})" if scenic else "At Sea",
                                        arrive=None, depart=None))
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    @staticmethod
    def fas_promo(avail: dict, type_code: str) -> list[str]:
        """The Free at Sea promotion the site pre-selects for this room type (e.g. ALL4CHO)."""
        for group in avail.get("offerGroups") or []:
            if group.get("code") != "FREE-AT-SEA":
                continue
            for p in group.get("promotionsInGroup") or []:
                promo = p.get("promotion") or {}
                if p.get("isDefaultInGroup") and type_code in (promo.get("stateroomTypes") or []):
                    return list(promo.get("promotionCodes") or [])
        return []

    @staticmethod
    def available_categories(avail: dict) -> list[tuple[str, str, float]]:
        """(room type, priced category code, all-in price pp) for every bookable category."""
        out = []
        for t in avail.get("stateroomTypesPricing") or []:
            for sp in t.get("stateroomsPricing") or []:
                for cp in sp.get("categoryPricing") or []:
                    opt = cp.get("standardOption") or {}
                    if cp.get("isAvailable") and opt.get("price") is not None and opt.get("pricedCategoryCode"):
                        out.append((t.get("code") or "", opt["pricedCategoryCode"], float(opt["price"])))
        return out

    @staticmethod
    def parse_summary(data: dict, guests: int) -> dict:
        """fare / taxes / Free at Sea charges per person from a price-summary reply."""
        room = ((data.get("staterooms") or [{}])[0].get("pricing") or {}) if isinstance(data, dict) else {}
        groups = {g.get("code"): g for g in room.get("priceGroups") or []}
        total = (data.get("total") or {}) if isinstance(data, dict) else {}
        taxes = total.get("taxesAndFees")
        if taxes is None:
            taxes = (groups.get("TAXES-AND-FEES") or {}).get("price")
        fare = (groups.get("STATEROOM") or {}).get("price")
        fas_group = groups.get("FREE-AT-SEA") or {}
        items = []
        for it in fas_group.get("priceItems") or []:
            per_guest = [g.get("price") for g in it.get("guests") or [] if g.get("price") is not None]
            items.append({
                "code": it.get("code"),
                "title": it.get("offerTitle") or it.get("title") or it.get("code"),
                "type": it.get("itemType"),
                "price_pp": float(per_guest[0]) if per_guest else round(float(it.get("totalPrice") or 0) / guests, 2),
                "free": bool(it.get("free")) or not it.get("totalPrice"),
            })
        return {
            "taxes_pp": round(float(taxes) / guests, 2) if taxes is not None else None,
            "fare_pp": round(float(fare) / guests, 2) if fare is not None else None,
            "fas_pp": round(float(fas_group.get("price") or 0) / guests, 2) if fas_group else None,
            "fas_items": items,
        }

    def fetch_type_summaries(self, package_id: str, avail: dict, guests: int) -> tuple[dict[str, dict], Optional[float]]:
        """One price-summary per room type (cheapest category, Free at Sea pre-selected, in parallel).

        Returns ({room type: summary}, taxes per person)."""
        cheapest: dict[str, tuple[str, float]] = {}
        for type_code, cat, price in self.available_categories(avail):
            if type_code not in cheapest or price < cheapest[type_code][1]:
                cheapest[type_code] = (cat, price)
        if not cheapest:
            return {}, None

        def one(type_code: str):
            cat, _ = cheapest[type_code]
            try:
                data = self.price_summary(package_id, type_code, cat, guests, self.fas_promo(avail, type_code))
                return type_code, self.parse_summary(data, guests)
            except ProviderError as exc:
                log.warning("NCL price summary %s/%s failed: %s", type_code, cat, exc)
                return type_code, None

        with ThreadPoolExecutor(max_workers=min(4, len(cheapest))) as pool:
            got = {t: s for t, s in pool.map(one, sorted(cheapest, key=lambda t: TYPE_ORDER.get(t, 99))) if s}
        taxes = next((s["taxes_pp"] for s in got.values() if s.get("taxes_pp") is not None), None)
        return got, taxes

    def map_categories(self, avail: dict, taxes: Optional[float], fas: dict[str, dict], guests: int,
                       source: str) -> list[Stateroom]:
        rows: list[tuple[int, float, str, Stateroom]] = []
        for t in avail.get("stateroomTypesPricing") or []:
            type_code = t.get("code") or ""
            category = TYPE_CATEGORY.get(type_code, "Other")
            summary = fas.get(type_code) or {}
            for sp in t.get("stateroomsPricing") or []:
                room = sp.get("stateroom") or {}
                if (room.get("guestCapacity") or 99) < guests:
                    continue  # e.g. solo studios / solo insides when quoting 2 guests
                title = room.get("title") or t.get("title") or type_code
                for cp in sp.get("categoryPricing") or []:
                    opt = cp.get("standardOption") or {}
                    code = opt.get("pricedCategoryCode") or opt.get("berthedCategoryCode")
                    if not code:
                        continue
                    all_in = _money(opt.get("price")) if cp.get("isAvailable") else None
                    notes = []
                    if all_in is None:
                        notes.append("Sold out" if cp.get("isSoldOut") or sp.get("isSoldOut") else "Not available online")
                    if room.get("isSailAway") or title.lower().startswith("guarantee"):
                        notes.append("Guarantee — NCL assigns the stateroom")
                    if opt.get("guestCapacity"):
                        notes.append(f"sleeps up to {opt['guestCapacity']}")
                    if summary.get("fas_pp"):
                        notes.append(f"Free at Sea adds ${summary['fas_pp']:,.2f} pp (fare unchanged)")
                    if all_in is not None and taxes is None:
                        notes.append("all-in price incl. taxes & port fees")
                    price = round(all_in - taxes, 2) if all_in is not None and taxes is not None else all_in
                    rows.append((TYPE_ORDER.get(type_code, 99), price if price is not None else 1e9, code, Stateroom(
                        category=category,
                        name=f"{title} ({code})",
                        code=code,
                        price_per_person=price,
                        taxes_fees_per_person=taxes,
                        sold_out=price is None,
                        notes="; ".join(notes) or None,
                        source=source,
                    )))
        seen, out = set(), []
        for _, _, _, room in sorted(rows, key=lambda r: r[:3]):
            if room.key not in seen:
                seen.add(room.key)
                out.append(room)
        return out

    def sailing_taxes(self, package_id: str, guests: int = 2) -> Optional[float]:
        """Taxes, fees & port expenses per person for a sailing (any category prices them)."""
        for type_code, cat in TAX_PROBES:
            try:
                data = self.price_summary(package_id, type_code, cat, guests)
            except ProviderError as exc:
                log.info("NCL taxes probe %s/%s for %s: %s", type_code, cat, package_id, exc)
                continue
            taxes = self.parse_summary(data, guests)["taxes_pp"]
            if taxes is not None:
                return taxes
        return None

    @staticmethod
    def lead_in_rooms(rows: list[dict], taxes: Optional[float], source: str) -> list[Stateroom]:
        """Fallback: the lowest price per room type from the sailings list."""
        out = []
        for r in sorted(rows, key=lambda r: TYPE_ORDER.get(r.get("stateroomType"), 99)):
            type_code = r.get("stateroomType") or ""
            if r.get("status") == "SOLO_GUEST_ONLY":
                continue
            all_in = _money(r.get("combinedPrice")) if r.get("status") == "AVAILABLE" else None
            price = round(all_in - taxes, 2) if all_in is not None and taxes is not None else all_in
            out.append(Stateroom(
                category=TYPE_CATEGORY.get(type_code, "Other"),
                name=f"{r.get('title') or type_code} (lowest fare)",
                code=type_code or None,
                price_per_person=price,
                taxes_fees_per_person=taxes,
                sold_out=price is None,
                notes=(r.get("statusText") if price is None else
                       "all-in price incl. taxes & port fees" if taxes is None else None),
                source=source,
            ))
        return out

    # ── Add-ons (Free at Sea) ────────────────────────────────────────────

    @staticmethod
    def fas_addons(fas: dict[str, dict]) -> list[AddOn]:
        """The Free at Sea perks and what NCL charges for each (package gratuities/fees)."""
        base = next((fas[t] for t in sorted(fas, key=lambda t: TYPE_ORDER.get(t, 99)) if fas[t].get("fas_items")), None)
        if not base:
            return []
        haven = fas.get("HAVEN") or {}
        out = []
        for it in base["fas_items"]:
            desc = ("Included free with Free at Sea." if it["free"] else
                    "Included with Free at Sea; NCL charges this per person for the sailing "
                    "(package gratuities/fees). Choosing Free at Sea doesn't change the cruise fare.")
            if haven.get("fas_pp") is not None and haven["fas_pp"] != base["fas_pp"]:
                desc += f" The Haven's Free at Sea charges total ${haven['fas_pp']:,.2f} pp."
            out.append(AddOn(
                kind=ADDON_KINDS.get(it["type"] or "", "other"),
                name=f"Free at Sea: {it['title']}",
                price=0.0 if it["free"] else it["price_pp"],
                price_unit="per_person",
                unit_label="per person, whole sailing",
                port=None,
                description=desc,
                source="ncl.com price summary",
            ))
        return out

    # ── Master catalog ───────────────────────────────────────────────────

    def iter_catalog(self) -> Iterator[CatalogSailing]:
        """Every bookable NCL cruise-only sailing with lead-in fares per room class.

        1 search call lists every itinerary (cruisetours skipped: same sailings plus a land
        package); 1 sailings call per itinerary (~640) gives each sailing's lowest all-in
        price per room type; then (catalog_taxes) 1 price-summary per sailing (~2,000) splits
        off the taxes. ~2,600 calls, 8 workers: roughly 8-11 minutes for the whole line.
        If a sailing's taxes can't be loaded, the taxes of the nearest sailing of the same
        itinerary are used (they differ by a few dollars at most); with none, prices stay all-in.
        """
        self.calls = 0
        itins = [i for i in self.search(bundles="cruise", limit=1000) if i.get("code")]
        if not itins:
            raise ProviderError("NCL search returned no itineraries")

        def fetch(itin):
            try:
                return itin, self.sailings(itin["code"], 2)
            except ProviderError as exc:
                log.warning("NCL catalog: sailings for %s failed: %s", itin["code"], exc)
                return itin, None

        rows: list[CatalogSailing] = []
        all_in: dict[str, dict[str, Optional[float]]] = {}
        with ThreadPoolExecutor(max_workers=CATALOG_WORKERS) as pool:
            for itin, data in pool.map(fetch, itins):
                if data:
                    for row, prices in self.catalog_rows(itin, data):
                        rows.append(row)
                        all_in[row.sailing_key] = prices

        taxes: dict[str, Optional[float]] = {}
        if self.catalog_taxes:
            with ThreadPoolExecutor(max_workers=CATALOG_WORKERS) as pool:
                for key, tax in zip([r.sailing_key for r in rows],
                                    pool.map(lambda r: self.sailing_taxes(r.sailing_key), rows)):
                    taxes[key] = tax
            self.fill_missing_taxes(rows, taxes)

        for row in sorted(rows, key=lambda r: (r.sail_date, r.ship_code or "", r.sailing_key)):
            tax = taxes.get(row.sailing_key)
            row.taxes_fees_per_person = tax
            row.prices = {k: (round(v - tax, 2) if v is not None and tax is not None else v)
                          for k, v in all_in[row.sailing_key].items()}
            yield row

    @staticmethod
    def fill_missing_taxes(rows: list[CatalogSailing], taxes: dict[str, Optional[float]]) -> None:
        by_itin: dict[tuple, list[CatalogSailing]] = {}
        for r in rows:
            by_itin.setdefault((r.ship_code, r.itinerary_name), []).append(r)
        for r in rows:
            if taxes.get(r.sailing_key) is not None:
                continue
            sibs = [s for s in by_itin.get((r.ship_code, r.itinerary_name), []) if taxes.get(s.sailing_key) is not None]
            if sibs:
                day = date.fromisoformat(r.sail_date).toordinal()
                near = min(sibs, key=lambda s: abs(date.fromisoformat(s.sail_date).toordinal() - day))
                taxes[r.sailing_key] = taxes[near.sailing_key]
            else:
                log.warning("NCL catalog: no taxes for %s %s; prices left all-in", r.ship, r.sail_date)

    def catalog_rows(self, itin: dict, data: dict) -> list[tuple[CatalogSailing, dict[str, Optional[float]]]]:
        """(row, all-in price per class) for each sailing of one itinerary."""
        details = data.get("itineraryDetails") or {}
        ship = details.get("ship") or itin.get("ship") or {}
        ports_of_call = details.get("portsOfCall") or itin.get("portsOfCall") or []
        code = details.get("code") or itin["code"]
        by_pkg: dict[str, list[dict]] = {}
        for r in data.get("pricingStateRooms") or []:
            if r.get("packageId") and not r.get("isPackage"):
                by_pkg.setdefault(str(r["packageId"]), []).append(r)
        out = []
        for pkg, prs in by_pkg.items():
            first = prs[0]
            prices: dict[str, Optional[float]] = {}
            suite: list[Optional[float]] = []
            for r in prs:
                type_code = r.get("stateroomType") or ""
                if r.get("status") == "SOLO_GUEST_ONLY":
                    continue
                price = _money(r.get("combinedPrice")) if r.get("status") == "AVAILABLE" else None
                if type_code in ("SUITE", "HAVEN"):
                    suite.append(price)
                if type_code in CATALOG_CLASSES:
                    prices[CATALOG_CLASSES[type_code]] = price
            if suite:  # "Suite" = cheapest full suite, whether a regular Suite or The Haven
                avail = [p for p in suite if p is not None]
                prices["Suite"] = min(avail) if avail else None
            out.append((CatalogSailing(
                cruise_line=self.cruise_line,
                sailing_key=pkg,
                ship=ship.get("title") or "",
                sail_date=(first.get("sailStartDate") or "")[:10],
                nights=_nights(first.get("sailStartDate"), first.get("sailEndDate")),
                ship_code=ship.get("code"),
                itinerary_name=details.get("title") or itin.get("title"),
                departure_port=(details.get("embarkationPort") or itin.get("embarkationPort") or {}).get("title"),
                ports=ordered_ports(first.get("sailingEventSequence"), ports_of_call),
                booking_url=self.booking_url(code, pkg),
                prices={},
                taxes_fees_per_person=None,
                currency=first.get("currencyCode") or "USD",
            ), prices))
        return out
