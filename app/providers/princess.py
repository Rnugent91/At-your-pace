"""Princess Cruises: live fares from princess.com's own JSON APIs (gw.api.princess.com).

Endpoints (the ones princess.com's cruise search and cruise-details pages call; the
reference-data ones were first spotted in the "cruisekit" project):
  * GET  /resdb/p1.0/ships | ports | metas        ship names, port names, room-type names
  * GET  /resdb/p1.0/products?...&light=false     every bookable cruise: voyage code, ship,
                                                   sail date, duration, ports in order
  * GET  /resdb/p1.0/itineraries?cruises=<voyage> day-by-day schedule with arrive/depart times
  * POST /caps/pc/pricing/v1/cruises              fares; with leadInBy "voyages" and no filters
                                                   it returns the lead fare per room class for
                                                   EVERY voyage in one ~6 MB call (the catalog)
  * POST /caps/pc/pricing/v1/cruises/<voyage>     every priced stateroom category of one voyage,
                                                   for both "Princess Standard" (BESTFARE) and
                                                   "Princess Plus" (BESTVALUE) fares
  * POST /caps/pc/pricing/v1/cruises/<voyage>/specials  the same with the Premier promo codes:
                                                   "Princess Premier" fares

Princess's US prices now include "government taxes and fees" (igvt) and "required cruise
fees and expenses" (rcfe). Each guest's `baseFare` is the fare without them, so
price_per_person = baseFare and taxes_fees_per_person = fare - baseFare.

Room types: a category code (e.g. "DE") belongs to a meta (I/O/B/M/S = Interior, Oceanview,
Balcony, Mini-Suite, Suite) and a sub-meta (e.g. B+D = "Deluxe Balcony"), both named by
/metas. Mini-suites are counted as "Suite" for the quote's category (Princess groups them with
suites), but kept as their own "Mini-Suite" class in the catalog.

All of this answers plain server requests today (gw.api.princess.com only wants the site's
public client-id headers). If it starts refusing a server IP, the same request is replayed
from inside a princess.com page through Cloudflare Browser Rendering (when configured).
"""

import logging
import re
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Iterator, Optional
from urllib.parse import urlencode

import httpx

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest
from .browser_fetch import fetch_json_in_page, page_text

log = logging.getLogger(__name__)

SITE = "https://www.princess.com"
API = "https://gw.api.princess.com/pcl-web/internal"
CLIENT_ID = "32e7224ac6cc41302f673c5f5d27b4ba"  # princess.com's public web client id
PRODUCTS_QUERY = "agencyCountry=US&cruiseType=C&voyageStatus=A&webDisplay=Y&promoFilter=all&light=false"

# Ship name → Princess ship code, as listed by /resdb/p1.0/ships (Sept 2026). The lookup
# also loads that list live, so a new ship works before this table is updated.
SHIP_CODES = {
    "caribbean princess": "CB",
    "coral princess": "CO",
    "crown princess": "KP",
    "diamond princess": "DI",
    "discovery princess": "XP",
    "emerald princess": "EP",
    "enchanted princess": "EX",
    "grand princess": "AP",
    "island princess": "IP",
    "majestic princess": "MJ",
    "regal princess": "GP",
    "royal princess": "RP",
    "ruby princess": "RU",
    "sapphire princess": "SA",
    "sky princess": "YP",
    "star princess": "ST",
    "sun princess": "SU",
}

META_CATEGORY = {"I": "Interior", "O": "Ocean View", "B": "Balcony", "M": "Suite", "S": "Suite"}
CATALOG_CLASS = {"I": "Interior", "O": "Ocean View", "B": "Balcony", "M": "Mini-Suite", "S": "Suite"}
META_NAMES = {"I": "Interior", "O": "Oceanview", "B": "Balcony", "M": "Mini-Suite", "S": "Suite"}

# Promo codes princess.com sends to /specials to price "Princess Premier" (from the site's
# /ube settings, features.premierPromos). Refreshed live when possible.
PREMIER_PROMOS = [
    "UGS", "URO", "UR*", "NWR", "RRA", "RNA", "NR*", "NN*", "RGR", "RK*", "US*", "RC*",
    "RGN*", "RCW", "RCX", "RKG", "RGT", "UN*", "RNB", "RG3", "KN*", "KF*",
]

FARE_STANDARD, FARE_PLUS = "BESTFARE", "BESTVALUE"

PLUS_DESC = (
    "Princess Plus fare: Plus Beverage Package (drinks up to $15 each), MedallionNet Wi-Fi "
    "(1 device), crew appreciation, premium desserts and 2 casual dining meals, and more."
)
PREMIER_DESC = (
    "Princess Premier fare: Premier Beverage Package (drinks up to $20 each), MedallionNet Wi-Fi "
    "(up to 4 devices), crew appreciation, unlimited specialty dining, photo package, "
    "reserved theater seating, and more."
)

# Published add-on pages (princess.com's own "from" prices; sailing-specific prices need a booking).
BEVERAGE_PAGE = SITE + "/cruise-dining/beverages"
CREW_PAGE = SITE + "/html/global/disclaimers/crew-appreciation/"
DINING_PAGES = {
    "Crown Grill": SITE + "/en-us/cruise-dining/crown-grill",
    "Sabatini's Italian Trattoria": SITE + "/en-us/cruise-dining/sabatinis-italian-trattoria",
}
NEWEST_SHIPS = {"SU", "ST"}  # Sun & Star Princess: higher published specialty dining prices

VOYAGE_RE = re.compile(r"(?:voyageCode|voyageId|voyage|cruiseCode|cruiseId|cruise)=([A-Za-z0-9]{4,6})\b", re.I)


def ship_code_for(ship: str, ships: Optional[dict[str, str]] = None) -> Optional[str]:
    """'Sun Princess' / 'sun' / 'SU' → 'SU'."""
    s = re.sub(r"\s+", " ", ship.strip())
    table = {**SHIP_CODES, **(ships or {})}
    if re.fullmatch(r"[A-Za-z]{2}", s) and s.upper() in table.values():
        return s.upper()
    key = s.lower()
    if key in table:
        return table[key]
    if not key.endswith(" princess") and f"{key} princess" in table:
        return table[f"{key} princess"]
    return None


def voyage_from_url(url: str) -> Optional[str]:
    m = VOYAGE_RE.search(url or "")
    return m.group(1).upper() if m else None


def ymd(yyyymmdd: str) -> Optional[str]:
    s = str(yyyymmdd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else None


def clock24(t: Optional[str]) -> Optional[str]:
    """'08:00 AM' → '08:00', '04:30 PM' → '16:30', '' → None."""
    t = (t or "").strip()
    if not t or t in {"00:00"}:
        return None
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(t, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


def guest_fare(category: dict, guests: int = 2) -> tuple[Optional[float], Optional[float]]:
    """Average (fare without taxes/fees, taxes/fees) per person for the first `guests` guests."""
    rows = sorted(category.get("guests") or [], key=lambda g: g.get("id") or 0)[: max(1, guests)]
    if not rows or rows[0].get("fare") is None:
        return None, None
    first = rows[0]
    tax = float(first["fare"]) - float(first["baseFare"]) if first.get("baseFare") is not None else 0.0
    bases, taxes = [], []
    for g in rows:
        fare = float(g.get("fare") or 0)
        base = float(g["baseFare"]) if g.get("baseFare") is not None else max(fare - tax, 0.0)
        bases.append(base)
        taxes.append(fare - base)
    n = len(rows)
    return round(sum(bases) / n, 2), round(sum(taxes) / n, 2)


def fares_by_type(pricing: dict) -> dict[str, dict[str, dict]]:
    """{fareType: {categoryCode: category}} from a pricing block."""
    out: dict[str, dict[str, dict]] = {}
    for fare in pricing.get("fares") or []:
        out[fare.get("fareType") or FARE_STANDARD] = {c["id"]: c for c in fare.get("categories") or [] if c.get("id")}
    return out


def category_submetas(fare: dict) -> dict[str, tuple[str, str]]:
    """categoryCode → (meta, subMeta) from the zones in a /cruises/<voyage> response."""
    out: dict[str, tuple[str, str]] = {}
    for meta in fare.get("metas") or []:
        mid = meta.get("id") or ""
        for zone in meta.get("zones") or []:
            for sub in zone.get("subMetas") or []:
                for code in sub.get("categories") or []:
                    out.setdefault(code, (mid, sub.get("id") or ""))
        for sub in meta.get("subMetas") or []:
            if sub.get("bestCategory"):
                out.setdefault(sub["bestCategory"], (mid, sub.get("id") or ""))
    return out


def upgrade_price(std: dict[str, dict], other: dict[str, dict], guests: int) -> Optional[float]:
    """The per-person cost of a fare upgrade: the most common (other − standard) fare difference."""
    diffs = []
    for code, cat in other.items():
        base = std.get(code)
        if not base:
            continue
        a, _ = guest_fare(base, guests)
        b, _ = guest_fare(cat, guests)
        if a is not None and b is not None and b > a:
            diffs.append(round(b - a, 2))
    if not diffs:
        return None
    return Counter(diffs).most_common(1)[0][0]


def _money(v: str) -> float:
    return float(v.replace(",", ""))


def parse_beverage_packages(text: str) -> tuple[list[tuple[str, float, str]], Optional[int]]:
    """[(package name, USD per person per day, blurb)] and the service-charge % from the beverages page."""
    out: list[tuple[str, float, str]] = []
    for m in re.finditer(r"Value of \$([\d,.]+) USD\*? per day View (\w+) Beverage Package Terms", text):
        tier = m.group(2)
        blurb = {"Premier": "Drinks up to $20 each: top-shelf spirits, reserve wines by the glass, cocktails, "
                            "specialty coffees and non-alcoholic drinks.",
                 "Plus": "Drinks up to $15 each: cocktails, wine by the glass, beer, specialty coffees, "
                         "sodas, bottled water, juices and smoothies."}.get(tier, "")
        out.append((f"{tier} Beverage Package", _money(m.group(1)), blurb))
    seen = {n for n, _, _ in out}
    for m in re.finditer(r"([A-Z][\w-]*(?: [A-Z][\w-]*)*) Package at \$([\d,.]+)/day", text):
        name = m.group(1).strip() + " Package"
        name = re.sub(r"^.*?(Zero-Alcohol|Classic Soda)", r"\1", name)
        if name in seen:
            continue
        seen.add(name)
        blurb = {"Zero-Alcohol Package": "Specialty coffees and teas, sodas, mocktails, bottled/premium water "
                                         "and Red Bull (no alcohol).",
                 "Classic Soda Package": "Sodas, fruit juices, mocktails and smoothies."}.get(name, "")
        out.append((name, _money(m.group(2)), blurb))
    sc = re.search(r"(\d+)% service charge", text, re.I)
    return out, int(sc.group(1)) if sc else None


def parse_specialty_dining(text: str, ship_code: Optional[str]) -> Optional[tuple[float, Optional[float]]]:
    """(adult, child 3-11) cover charge for this ship from a Princess specialty restaurant page."""
    m = re.search(r"\$([\d.]+)/adult, \$([\d.]+)/child \(3-11\); Sun & Star Princess only: "
                  r"\$([\d.]+)/adult, \$([\d.]+)/child", text)
    if m:
        if ship_code in NEWEST_SHIPS:
            return float(m.group(3)), float(m.group(4))
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"\$([\d.]+)/adult(?:, \$([\d.]+)/child)?", text)
    if m:
        return float(m.group(1)), float(m.group(2)) if m.group(2) else None
    return None


def parse_crew_appreciation(text: str) -> list[tuple[str, float]]:
    """[(stateroom group, USD per guest per day)] from Princess's crew appreciation disclaimer."""
    out = []
    for label, pat in (("Suites", r"\bSuites \$([\d.]+) USD per person per day"),
                       ("Mini-suites, Cabanas and Reserve Collection",
                        r"Mini Suites, Cabanas, and Reserve Collection \$([\d.]+) USD per person per day"),
                       ("All other staterooms", r"All other stateroom types \$([\d.]+) USD per person per day")):
        m = re.search(pat, text)
        if m:
            out.append((label, float(m.group(1))))
    return out


def excursion_addons(data: dict, port: str, source: str) -> list[AddOn]:
    out = []
    for ex in (data or {}).get("excursions") or []:
        try:
            price = float(ex.get("price"))
        except (TypeError, ValueError):
            continue
        bits = []
        if ex.get("duration"):
            bits.append(f"{ex['duration']} hours")
        if ex.get("activityLevel"):
            bits.append(f"activity: {ex['activityLevel']}")
        food = (ex.get("foodService") or "").strip()
        if food and food.lower() not in {"no", "n", "none"}:
            bits.append(food.lower())
        out.append(AddOn(
            kind="excursion",
            name=(ex.get("name") or ex.get("id") or "").strip(),
            price=price,
            price_unit="per_person",
            unit_label="per adult",
            port=port,
            description="; ".join(bits) or None,
            source=source,
        ))
    return out


class PrincessProvider:
    name = "Princess (live)"
    cruise_line = "Princess"

    def __init__(self, cf: Optional[CloudflareBrowser] = None, http: Optional[httpx.Client] = None,
                 api_base: str = API):
        self.cf = cf
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.api = api_base.rstrip("/")
        self.session_id = str(uuid.uuid4())
        self._blocked = False
        self._cache: dict[str, object] = {}
        self.calls = 0  # HTTP calls made (for sync logging)

    def handles(self, cruise_line: str) -> bool:
        return "princess" in cruise_line.lower()

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "accept": "application/json, text/plain, */*",
            "appid": (
                '{"agencyId":"DIRPB","cruiseLineCode":"PCL","sessionId":"' + self.session_id
                + '","systemId":"PB","gdsCookie":"CO=US"}'
            ),
            "bookingcompany": "PC",
            "productcompany": "PC",
            "pcl-client-id": CLIENT_ID,
            "reqsrc": "W",
            "origin": SITE,
            "referer": SITE + "/",
            "user-agent": USER_AGENT,
        }

    def _json(self, method: str, path: str, body: Optional[dict] = None, attempts: int = 3):
        url = self.api + path
        if not self._blocked:
            for attempt in range(1, attempts + 1):
                self.calls += 1
                try:
                    resp = self.http.request(method, url, json=body, headers=self._headers())
                except httpx.HTTPError as exc:
                    if attempt < attempts:
                        time.sleep(attempt)
                        continue
                    if self.cf is None or not self.cf.configured:
                        raise ProviderError(f"Princess request {path} failed: {exc}") from exc
                    log.warning("Princess direct call %s failed (%s); trying Cloudflare browser", path, exc)
                    break
                if resp.status_code == 429 or resp.status_code >= 502:
                    if attempt < attempts:
                        time.sleep(float(resp.headers.get("retry-after") or 0) or 2 * attempt)
                        continue
                if resp.status_code in (401, 403, 429) and self.cf is not None and self.cf.configured:
                    log.warning("Princess refused %s (%s); switching to Cloudflare browser", path, resp.status_code)
                    self._blocked = True
                    break
                if resp.status_code >= 400:
                    raise ProviderError(f"Princess request {path} failed: HTTP {resp.status_code}")
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ProviderError(f"Princess request {path} returned non-JSON") from exc
        headers = {k: v for k, v in self._headers().items() if k not in {"origin", "referer", "user-agent"}}
        self.calls += 1
        return fetch_json_in_page(self.cf, SITE + "/cruise-search/", url, method, headers, body,
                                  marker="qp-princess", label=f"Princess request {path}")

    def _cached(self, key: str, loader):
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    # ── Reference data ───────────────────────────────────────────────────

    def ships(self) -> dict[str, str]:
        """Ship code → name."""
        def load():
            try:
                data = self._json("GET", "/resdb/p1.0/ships")
                return {s["id"]: s["name"] for s in data.get("ships") or [] if s.get("id")}
            except ProviderError as exc:
                log.warning("Princess ships list failed: %s", exc)
                return {code: name.title() for name, code in SHIP_CODES.items()}
        return self._cached("ships", load)

    def ports(self) -> dict[str, str]:
        def load():
            try:
                data = self._json("GET", "/resdb/p1.0/ports")
                return {p["id"]: p["name"] for p in data.get("ports") or [] if p.get("id")}
            except ProviderError as exc:
                log.warning("Princess ports list failed: %s", exc)
                return {}
        return self._cached("ports", load)

    def room_names(self) -> dict[str, str]:
        """'BD' (meta+subMeta) → 'Deluxe Balcony', plus 'B' → 'Balcony'."""
        def load():
            out = dict(META_NAMES)
            try:
                data = self._json("GET", "/resdb/p1.0/metas")
            except ProviderError as exc:
                log.warning("Princess metas failed: %s", exc)
                return out
            for meta in data.get("metas") or []:
                out[meta.get("id")] = meta.get("name") or out.get(meta.get("id"), "")
                for sub in meta.get("subMetas") or []:
                    out[sub.get("id")] = sub.get("name")
            return out
        return self._cached("metas", load)

    def ship_categories(self, ship_code: str, version) -> dict[str, dict]:
        """categoryCode → {name, meta, subMeta} for one ship version (/ships/<code>/<ver>/categories)."""
        def load():
            try:
                data = self._json("GET", f"/resdb/p1.0/ships/{ship_code}/{version}/categories")
            except ProviderError as exc:
                log.info("Princess categories for %s/%s failed: %s", ship_code, version, exc)
                return {}
            return {c["id"]: c for c in data.get("categories") or [] if c.get("id")}
        if not ship_code or version in (None, ""):
            return {}
        return self._cached(f"cats:{ship_code}:{version}", load)

    def premier_promos(self) -> list[str]:
        def load():
            try:
                data = self._json("GET", "/ube/p1.0/ube?env=prod&country=US")
                promos = (((data.get("ube") or {}).get("settings") or {}).get("features") or {}).get("premierPromos")
                if promos:
                    return list(promos)
            except ProviderError as exc:
                log.info("Princess ube settings failed: %s", exc)
            return PREMIER_PROMOS
        return self._cached("premier", load)

    def products(self) -> list[dict]:
        def load():
            data = self._json("GET", "/resdb/p1.0/products?" + PRODUCTS_QUERY)
            return data.get("products") or []
        return self._cached("products", load)

    def cruises(self) -> list[tuple[dict, dict]]:
        """Every bookable (product, cruise) pair."""
        return [(p, c) for p in self.products() for c in p.get("cruises") or []]

    # ── Pricing requests ─────────────────────────────────────────────────

    @staticmethod
    def _booking(guests: int, promos: Optional[list[str]] = None) -> dict:
        return {
            "currencyCode": "USD",
            "guests": [{"country": "US", "homeCity": "LAX"} for _ in range(max(1, min(guests, 5)))],
            "promos": promos or [],
            "couponCodes": [],
            "bookingAgency": {"id": "DIRPB", "bookingCompany": "PC", "currency": "USD", "country": "US"},
        }

    @staticmethod
    def _flags(**extra) -> dict:
        return {
            "additionalGuestFare": True, "averageFare": True, "brochureFare": True, "averageBrochureFare": True,
            "includeMisc": True, "fareType": FARE_STANDARD, "roundUpFare": True, "subMeta": True, "zones": True,
            "includeTfpe": False, **extra,
        }

    def voyage_pricing(self, voyage: str, guests: int = 2) -> dict:
        body = {
            "booking": self._booking(guests),
            "filters": {"availabilities": ["Y", "G", "B"], "cruises": [], "cruiseType": "C", "meta": "I",
                        "itinPorts": [], "subTrades": []},
            "leadInBy": "itins",
            "retrieveFlags": self._flags(),
        }
        return self._first_pricing(self._json("POST", f"/caps/pc/pricing/v1/cruises/{voyage}", body), voyage)

    def premier_pricing(self, voyage: str, guests: int = 2) -> dict:
        promos = self.premier_promos()
        body = {
            "booking": self._booking(guests, promos),
            "filters": {"availabilities": ["Y", "G", "B"], "cruises": [], "cruiseType": "C", "meta": "I",
                        "promoFilters": promos},
            "leadInBy": "itins",
            "retrieveFlags": self._flags(),
        }
        return self._first_pricing(self._json("POST", f"/caps/pc/pricing/v1/cruises/{voyage}/specials", body), voyage)

    @staticmethod
    def _first_pricing(data: dict, voyage: str) -> dict:
        for product in (data or {}).get("products") or []:
            for cruise in product.get("cruises") or []:
                if cruise.get("id") == voyage:
                    return cruise.get("pricing") or {}
        return {}

    def all_lead_fares(self) -> dict[str, dict]:
        """voyage → pricing block with the lead category per room class, for every voyage (one call)."""
        body = {
            "booking": self._booking(2),
            "filters": {"availabilities": ["Y", "G", "B"], "cruises": [], "cruiseType": "C", "itinPorts": [],
                        "subTrades": []},
            "leadInBy": "voyages",
            "retrieveFlags": self._flags(subMeta=False, zones=False),
        }
        data = self._json("POST", "/caps/pc/pricing/v1/cruises", body)
        return {c["id"]: c.get("pricing") or {} for p in data.get("products") or [] for c in p.get("cruises") or []
                if c.get("id")}

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        guests = max(1, req.adults + req.children)
        warnings: list[str] = []
        product, cruise = self.find_cruise(req, warnings)
        voyage = cruise["id"]
        v = cruise.get("voyage") or {}
        ships = self.ships()
        ports = self.ports()
        ship_name = ships.get((v.get("ship") or {}).get("id"), req.ship)
        sail_date = ymd(v.get("sailDate") or cruise.get("startDate")) or req.sail_date
        details_url = f"{SITE}/cruise-search/details/?voyageCode={voyage}"

        research = SailingResearch(
            cruise_line=self.cruise_line,
            ship=ship_name,
            sail_date=sail_date,
            nights=v.get("duration"),
            departure_port=ports.get(v.get("startPortId")),
            itinerary_name=(product.get("name") or "").replace("  ", " ") or None,
            itinerary=[],
            currency="USD",
            staterooms=[],
            addons=[],
            sources=[details_url],
            warnings=warnings,
        )
        warnings = research.warnings  # pydantic copied the list

        try:
            research.itinerary = self.fetch_itinerary(voyage, sail_date, ports)
        except ProviderError as exc:
            warnings.append(f"Couldn't load Princess's day-by-day itinerary ({exc}).")

        try:
            pricing = self.voyage_pricing(voyage, guests)
        except ProviderError as exc:
            raise ProviderError(f"Princess fares for {voyage} failed: {exc}") from exc
        research.currency = pricing.get("fareCurrency") or "USD"
        fares = fares_by_type(pricing)
        premier: dict[str, dict] = {}
        try:
            premier = fares_by_type(self.premier_pricing(voyage, guests)).get(FARE_STANDARD, {})
        except ProviderError as exc:
            log.info("Princess Premier fares for %s failed: %s", voyage, exc)

        ship = v.get("ship") or {}
        cats = self.ship_categories(ship.get("id"), ship.get("version"))
        research.staterooms = self.map_staterooms(pricing, fares, premier, guests, details_url, cats)
        if not research.staterooms:
            warnings.append("Princess returned no bookable stateroom categories for this sailing.")
        elif guests != 2:
            warnings.append(f"Princess prices are the average per person for {guests} guests in the stateroom.")
        research.addons = self.package_addons(fares, premier, guests, research.nights, details_url)
        try:
            research.addons += self.fetch_addons(voyage, v.get("ports") or [], ship.get("id"), ports, warnings)
        except Exception as exc:  # add-ons must never fail research
            log.warning("Princess add-ons for %s failed: %s", voyage, exc)
            warnings.append(f"Couldn't load Princess add-ons ({exc}).")
        warnings.append(
            "Princess fares shown are 'Princess Standard'. Cruise fare excludes government taxes and "
            "Princess's required cruise fees, which are listed separately."
        )
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def find_cruise(self, req: ResearchRequest, warnings: list[str]) -> tuple[dict, dict]:
        cruises = self.cruises()
        voyage = voyage_from_url(req.booking_url)
        if voyage:
            for p, c in cruises:
                if c.get("id", "").upper() == voyage:
                    return p, c
            warnings.append(f"Voyage {voyage} from the booking link isn't bookable on princess.com; matched by ship and date.")
        names = {name.lower(): code for code, name in self.ships().items()}
        code = ship_code_for(req.ship, names)
        if not code:
            raise ProviderError(f"Unknown Princess ship '{req.ship}'")
        day = req.sail_date.replace("-", "")
        hits = [(p, c) for p, c in cruises
                if ((c.get("voyage") or {}).get("ship") or {}).get("id") == code
                and ((c.get("voyage") or {}).get("sailDate") or c.get("startDate")) == day]
        if not hits:
            raise ProviderError(f"No Princess sailing found for {req.ship} ({code}) on {req.sail_date}")
        hits.sort(key=self._cruise_rank)
        if len(hits) > 1:
            others = ", ".join(f"{c['id']} ({(c.get('voyage') or {}).get('duration')} nights, {p.get('name')})"
                               for p, c in hits[1:])
            warnings.append(f"Other Princess voyages also start that day: {others}. Paste the princess.com "
                            "cruise link to price one of those instead.")
        return hits[0]

    @staticmethod
    def _cruise_rank(pc: tuple[dict, dict]) -> tuple:
        """Prefer the ship's own voyage (e.g. 'G639') over combined/partial ones ('G639A')."""
        _, c = pc
        cid = c.get("id") or ""
        return (0 if re.fullmatch(r"[A-Z0-9]\d{3}", cid) else 1, (c.get("voyage") or {}).get("duration") or 99, cid)

    # ── Itinerary ────────────────────────────────────────────────────────

    def fetch_itinerary(self, voyage: str, sail_date: str, ports: dict[str, str]) -> list[ItineraryDay]:
        data = self._json("GET", "/resdb/p1.0/itineraries?" + urlencode({"cruises": voyage}))
        for c in data.get("cruises") or []:
            if c.get("id") == voyage:
                return self.map_days(c.get("itineraries") or [], data.get("ports") or [], sail_date, ports)
        return []

    @staticmethod
    def map_days(stops: list[dict], stop_ports: list[dict], sail_date: str,
                 port_names: dict[str, str]) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        named = [p for p in stop_ports if p.get("id")]
        known = sorted(set(port_names) | {p["id"] for p in named}, key=len, reverse=True)
        out, n = [], 0
        for stop in stops:
            day_in = stop.get("dayIn")
            day_date = ymd(stop.get("arrivalDt")) or (
                (start + timedelta(days=day_in)).isoformat() if isinstance(day_in, int) else None)
            day_no = (day_in + 1) if isinstance(day_in, int) else len(out) + 1
            if not stop.get("id"):
                out.append(ItineraryDay(day=day_no, date=day_date, port="At Sea", arrive=None, depart=None))
                continue
            name = None
            if n < len(named):
                name = named[n].get("name")
            n += 1
            if not name:
                pid = next((k for k in known if stop["id"].startswith(k)), None)
                name = port_names.get(pid) if pid else None
            out.append(ItineraryDay(
                day=day_no,
                date=day_date,
                port=name or stop["id"],
                arrive=clock24(stop.get("arrivalTime")),
                depart=clock24(stop.get("departTime")),
            ))
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    def map_staterooms(self, pricing: dict, fares: dict[str, dict[str, dict]], premier: dict[str, dict],
                       guests: int, source: str, ship_cats: Optional[dict[str, dict]] = None) -> list[Stateroom]:
        names = self.room_names()
        ship_cats = ship_cats or {}
        std = fares.get(FARE_STANDARD) or {}
        plus = fares.get(FARE_PLUS) or {}
        fare_block = next((f for f in pricing.get("fares") or [] if f.get("fareType") == FARE_STANDARD),
                          (pricing.get("fares") or [{}])[0] if pricing.get("fares") else {})
        subs = category_submetas(fare_block)
        rooms: list[tuple[float, Stateroom]] = []
        for code, cat in std.items():
            info = ship_cats.get(code) or {}
            if info.get("meta"):
                meta, sub = info["meta"], info.get("subMeta") or ""
            else:
                meta, sub = subs.get(code, (code[:1] if code[:1] in META_CATEGORY else "", ""))
            base, tax = guest_fare(cat, guests)
            notes = []
            p_base = guest_fare(plus[code], guests)[0] if code in plus else None
            r_base = guest_fare(premier[code], guests)[0] if code in premier else None
            if p_base is not None and base is not None and p_base > base:
                notes.append(f"Princess Plus fare ${p_base:,.0f} pp")
            if r_base is not None and base is not None and r_base > base:
                notes.append(f"Princess Premier fare ${r_base:,.0f} pp")
            if cat.get("guests") and (cat["guests"][0].get("ssv") or 0) and (p_base is None or p_base == base):
                notes.append("fare already includes Princess package perks")
            if cat.get("status") == "G":
                notes.append("guarantee (Princess assigns the stateroom)")
            avail = cat.get("availableCabins") or cat.get("availability")
            if avail and str(avail).isdigit() and int(avail) <= 3:
                notes.append(f"only {avail} left")
            rooms.append((base if base is not None else 1e12, Stateroom(
                category=META_CATEGORY.get(meta, "Other"),
                name=f"{info.get('name') or names.get(meta + sub) or names.get(meta) or 'Stateroom'} ({code})",
                code=code,
                price_per_person=base,
                taxes_fees_per_person=tax,
                sold_out=base is None,
                notes="; ".join(notes) or None,
                source=source,
            )))
        # Room types Princess lists as sold out on this voyage.
        for meta in fare_block.get("metas") or []:
            for sub in meta.get("subMetas") or []:
                if sub.get("status") == "N" and not sub.get("bestCategory"):
                    key = (meta.get("id") or "") + (sub.get("id") or "")
                    rooms.append((1e13, Stateroom(
                        category=META_CATEGORY.get(meta.get("id"), "Other"),
                        name=names.get(key) or names.get(meta.get("id")) or key,
                        code=None,
                        price_per_person=None,
                        taxes_fees_per_person=None,
                        sold_out=True,
                        notes=sub.get("statusMessage") or "Sold out",
                        source=source,
                    )))
        order = {"Interior": 0, "Ocean View": 1, "Balcony": 2, "Suite": 3, "Other": 4}
        rooms.sort(key=lambda r: (order[r[1].category], r[0]))
        seen, out = set(), []
        for _, room in rooms:
            if room.code is None and room.name in seen:
                continue  # a sold-out room type that also has a priced category
            seen.add(room.name)
            out.append(room)
        return out

    @staticmethod
    def package_addons(fares: dict[str, dict[str, dict]], premier: dict[str, dict], guests: int,
                       nights: Optional[int], source: str) -> list[AddOn]:
        std = fares.get(FARE_STANDARD) or {}
        out = []
        for name, other, desc in (("Princess Plus", fares.get(FARE_PLUS) or {}, PLUS_DESC),
                                  ("Princess Premier", premier, PREMIER_DESC)):
            price = upgrade_price(std, other, guests)
            if price is None:
                continue
            per_day = f" (about ${price / nights:,.2f} per person per day)" if nights else ""
            out.append(AddOn(
                kind="beverage",
                name=f"{name} (fare upgrade)",
                price=price,
                price_unit="per_person",
                unit_label="per person, whole cruise",
                port=None,
                description=f"{desc} Priced as the difference from the Princess Standard fare{per_day}.",
                source=source,
            ))
        return out

    # ── Add-ons ──────────────────────────────────────────────────────────

    def _page(self, url: str) -> str:
        """A princess.com page's text; through Cloudflare if the direct request is refused."""
        if not self._blocked:
            self.calls += 1
            try:
                resp = self.http.get(url, headers={"user-agent": USER_AGENT, "accept": "text/html"})
                if resp.status_code == 200:
                    return page_text(resp.text)
                err = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                err = str(exc)
        else:
            err = "blocked"
        if self.cf is None or not self.cf.configured:
            raise ProviderError(f"{url} failed: {err}")
        self.calls += 1
        try:
            return page_text(self.cf.content({"url": url, "gotoOptions": {"waitUntil": "domcontentloaded",
                                                                          "timeout": 30000}}))
        except BrowserRenderingError as exc:
            raise ProviderError(f"{url} failed in browser: {exc}") from exc

    def fetch_addons(self, voyage: str, port_codes: list[str], ship_code: Optional[str],
                     port_names: dict[str, str], warnings: list[str]) -> list[AddOn]:
        """Drink packages, specialty dining, crew appreciation (published prices) and this voyage's
        shore excursions per port, fetched in parallel."""
        ports = [p for i, p in enumerate(port_codes) if p and p not in port_codes[:i]]
        pages = {"bev": BEVERAGE_PAGE, "crew": CREW_PAGE, **{f"dine:{k}": u for k, u in DINING_PAGES.items()}}

        def page(url):
            try:
                return self._page(url)
            except ProviderError as exc:
                log.info("Princess page %s failed: %s", url, exc)
                return None

        def excursions(code):
            try:
                return self._json("GET", f"/db-excursion/p1.0/ports/{code}/excursions?"
                                  + urlencode({"voyageId": voyage}), attempts=2)
            except ProviderError as exc:
                log.info("Princess excursions %s/%s failed: %s", voyage, code, exc)
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            page_futs = {k: pool.submit(page, u) for k, u in pages.items()}
            ex_futs = {c: pool.submit(excursions, c) for c in ports}
            texts = {k: f.result() for k, f in page_futs.items()}
            ex_data = {c: f.result() for c, f in ex_futs.items()}

        out: list[AddOn] = []
        missing: list[str] = []
        published: list[str] = []

        if texts["bev"]:
            packages, sc = parse_beverage_packages(texts["bev"])
            note = (f"Published price; excludes Princess's {sc}% service charge." if sc
                    else "Published price; excludes Princess's service charge.")
            for name, price, blurb in packages:
                listed = " Princess lists it as a value of this amount per day." if "Beverage" in name else ""
                out.append(AddOn(kind="beverage", name=name, price=price, price_unit="per_person_per_day",
                                 unit_label="per person, per day", port=None,
                                 description=f"{blurb}{listed} {note} Must be bought for every day of the "
                                             "cruise.".strip(),
                                 source=BEVERAGE_PAGE))
            if packages:
                published.append("drink packages")
        else:
            packages = []
        if not packages:
            missing.append("drink packages")

        for name, url in DINING_PAGES.items():
            text = texts.get(f"dine:{name}")
            price = parse_specialty_dining(text, ship_code) if text else None
            if not price:
                missing.append(name)
                continue
            adult, child = price
            desc = "Specialty restaurant cover charge per meal, plus 20% service charge; included with Princess Premier."
            if child:
                desc += f" Children 3-11: ${child:,.2f}."
            out.append(AddOn(kind="dining", name=name, price=adult, price_unit="per_person",
                             unit_label="per adult, per meal", port=None, description=desc, source=url))
            published.append(name)

        crew = parse_crew_appreciation(texts["crew"]) if texts["crew"] else []
        for label, price in crew:
            out.append(AddOn(kind="other", name=f"Crew appreciation (gratuities) - {label}", price=price,
                             price_unit="per_person_per_day", unit_label="per guest, per day", port=None,
                             description="Added daily to the onboard account (adjustable); included with "
                                         "Princess Plus and Premier.",
                             source=CREW_PAGE))
        if crew:
            published.append("crew appreciation")
        else:
            missing.append("crew appreciation")

        no_ex = []
        for code in ports:
            name = port_names.get(code, code)
            found = excursion_addons(ex_data.get(code) or {}, name,
                                     f"{SITE}/cruise-search/details/?voyageCode={voyage}")
            if found:
                out.extend(found)
            elif ex_data.get(code) is None:
                no_ex.append(name)

        if published:
            warnings.append("Princess " + ", ".join(published) + " are published fleet-wide prices from "
                            "princess.com, not quoted for this sailing.")
        warnings.append("Princess doesn't publish MedallionNet Wi-Fi a la carte prices online (only in Manage "
                        "Booking); Wi-Fi is included in Princess Plus (1 device) and Premier (4 devices).")
        if missing:
            warnings.append(f"Couldn't load Princess add-on prices for: {', '.join(missing)}.")
        if no_ex:
            warnings.append(f"Couldn't load Princess shore excursions for: {', '.join(no_ex)}.")
        return out

    # ── Master catalog ───────────────────────────────────────────────────

    def iter_catalog(self) -> Iterator[CatalogSailing]:
        """Every bookable voyage with lead-in class fares: 4 small reference calls + 2 bulk calls."""
        ships, ports = self.ships(), self.ports()
        leads = self.all_lead_fares()
        seen = set()
        for product, cruise in self.cruises():
            vid = cruise.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            row = self.catalog_row(product, cruise, leads.get(vid) or {}, ships, ports)
            if row:
                yield row

    def catalog_row(self, product: dict, cruise: dict, pricing: dict, ships: dict[str, str],
                    ports: dict[str, str]) -> Optional[CatalogSailing]:
        v = cruise.get("voyage") or {}
        sail_date = ymd(v.get("sailDate") or cruise.get("startDate"))
        if not sail_date:
            return None
        ship_code = (v.get("ship") or {}).get("id")
        stops = v.get("ports") or []
        port_list = []
        for pid in stops[1:-1]:
            name = ports.get(pid, pid)
            if not port_list or port_list[-1] != name:
                port_list.append(name)
        prices: dict[str, Optional[float]] = {}
        taxes = None
        fare = next((f for f in pricing.get("fares") or [] if f.get("fareType") == FARE_STANDARD), None)
        if fare:
            cats = {c["id"]: c for c in fare.get("categories") or [] if c.get("id")}
            best = None
            for meta in fare.get("metas") or []:
                cls = CATALOG_CLASS.get(meta.get("id"))
                if not cls:
                    continue
                cat = cats.get(meta.get("bestCategory") or "")
                if cat:
                    base, tax = guest_fare(cat, 2)
                    prices[cls] = base
                    if base is not None and (best is None or base < best):
                        best, taxes = base, tax
                elif meta.get("status") == "N":  # "Category sold out" (E = not on this ship)
                    prices[cls] = None
        return CatalogSailing(
            cruise_line=self.cruise_line,
            sailing_key=cruise["id"],
            ship=ships.get(ship_code, ship_code or ""),
            ship_code=ship_code,
            sail_date=sail_date,
            nights=v.get("duration"),
            itinerary_name=re.sub(r"\s+", " ", product.get("name") or "").strip() or None,
            departure_port=ports.get(v.get("startPortId") or (stops[0] if stops else "")),
            ports=port_list,
            booking_url=f"{SITE}/cruise-search/details/?voyageCode={cruise['id']}",
            prices=prices,
            taxes_fees_per_person=taxes,
            currency=pricing.get("fareCurrency") or "USD",
        )
