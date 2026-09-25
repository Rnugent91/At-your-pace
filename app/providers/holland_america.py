"""Holland America Line: live fares from hollandamerica.com's own JSON endpoints.

Endpoints (the ones the "Find a Cruise" and cruise-details pages call):
  * GET /search/halcruisesearch       Solr cruise search (no auth). One document per
                                       sailing (cruiseId, e.g. "J711") with ship, dates, ports
                                       and lead fares per room class for each fare type:
                                         price_USD_<room>_RESTRICTED_d  "Cruise Only: Our Lowest Fare"
                                         price_USD_<room>_BASEPRICE_d   "Cruise Only: Flexible Fare"
                                         price_USD_<room>_anonymous_d   the default, "Have It All" fare
                                       (-1 = room class not on this ship, 0 = sold out). Prices
                                       include taxes; taxExpenses_USD_* is the per-person tax part.
  * GET /api/v2/price/cruise/<id>      every stateroom category of one sailing (e.g. "Obstructed
                                       Verandah" NS_VN_OVN) with its Cruise Only lowest fare and
                                       its Have It All fare: price, basePrice (no taxes) and tax.
  * GET /bin/carnival/hal/us/en<contentPath>/itinerarylistview.v2.json
                                       day-by-day itinerary with arrive/depart times.

Room classes (HAL id → our category): IN Inside → Interior; OV Ocean View; VN Verandah and
LA Lanai → Balcony; VS Vista Suite, SS Signature Suite, NS Neptune Suite, PH Pinnacle Suite
→ Suite.

Have It All (HIA) is HAL's package fare (Signature Beverage Package, specialty dining, Wi-Fi
and shore excursion credit). Staterooms are priced at the Cruise Only fare and HIA is offered
as an add-on priced as the per-person difference.

Cruisetours (cruise + land tour, `tourId` set) are left out of the catalog: they share the
ship's sailing, and their departDate can be the land start.

Everything answers plain server requests with a browser User-Agent today. If
hollandamerica.com starts refusing a server IP (Akamai), the same call is replayed from
inside a hollandamerica.com page through Cloudflare Browser Rendering (when configured).
"""

import logging
import re
import time
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

SITE = "https://www.hollandamerica.com"
LOCALE_PATH = "/en/us"

# Ship name → HAL ship code, as listed by the cruise search's ship facet (Sept 2026).
SHIP_CODES = {
    "eurodam": "ED",
    "koningsdam": "KO",
    "nieuw amsterdam": "NA",
    "nieuw statendam": "NS",
    "noordam": "NO",
    "oosterdam": "OS",
    "rotterdam": "RN",
    "volendam": "VO",
    "westerdam": "WE",
    "zaandam": "AA",
    "zuiderdam": "UU",
}

ROOM_CATEGORY = {
    "IN": "Interior", "OV": "Ocean View", "VN": "Balcony", "LA": "Balcony",
    "VS": "Suite", "SS": "Suite", "NS": "Suite", "PH": "Suite",
}
# Catalog classes: room id → class name (lowest fare wins within a class).
CATALOG_CLASS = {
    "IN": "Interior", "OV": "Ocean View", "VN": "Balcony", "LA": "Balcony",
    "VS": "Suite", "SS": "Suite", "NS": "Neptune Suite", "PH": "Neptune Suite",
}
ROOM_IDS = list(ROOM_CATEGORY)

HIA_PROMO = re.compile(r"^N\d")  # Have It All promos: N1_HAVEITALL, N2 (early booking bonus), ...
HIA_DESC = (
    "Have It All package fare: Signature Beverage Package, specialty dining, Wi-Fi and a "
    "shore excursion credit, with a refundable (flexible) deposit."
)

SEARCH_FIELDS = [
    "cruiseId", "itineraryId", "shipId", "shipName", "departDate", "arrivalDate", "duration", "contentPath",
    "name", "nightName", "embarkPortName", "disembarkPortName", "portOfCallIds", "portsOfCall", "soldOut", "tourId",
    "taxExpenses_USD_RESTRICTED", "taxExpenses_USD_BASEPRICE", "taxExpenses_USD_anonymous",
] + [f"price_USD_{r}_{ft}_d" for r in ROOM_IDS for ft in ("RESTRICTED", "BASEPRICE", "anonymous")]

URL_RE = re.compile(r"/find-a-cruise/([a-z0-9]+)/([a-z0-9]+)", re.I)
URL_PARAM_RE = re.compile(r"(?:cruiseId|voyageId|voyageCode|cruiseCode)=([A-Za-z0-9]{3,6})\b", re.I)
NON_PORT_IDS = {"ATSEADAY"}


# Published add-on pages (hollandamerica.com's own prices; sailing-specific prices need a booking).
PAGES = {
    "beverage": "/onboard-packages/beverage-packages",
    "wifi": "/onboard-packages/cruise-ship-wifi",
    "crew": "/plan-a-cruise/get-ready-for-your-cruise/faq/know-before-you-go",
}
DINING_PAGES = {
    "Pinnacle Grill": "/onboard-experiences/dining/pinnacle-grill",
    "Canaletto": "/onboard-experiences/dining/canaletto",
    "Tamarind": "/onboard-experiences/dining/tamarind",
    "Morimoto by Sea": "/onboard-experiences/dining/morimoto-by-sea",
    "Sel de Mer": "/onboard-experiences/dining/sel-de-mer",
}
BEVERAGE_BLURBS = {
    "Elite Beverage Package": "Premium spirits, cocktails and wines up to US$16 each plus everything in Signature "
                              "and Quench; 15 alcoholic drinks a day, unlimited non-alcoholic.",
    "Signature Beverage Package": "Beer, spirits, cocktails and wines by the glass up to US$12 each plus Quench; "
                                  "15 drinks a day.",
    "Quench": "Non-alcoholic: Coca-Cola products, espresso drinks, juices, mocktails and bottled water; "
              "15 drinks a day.",
}
WIFI_BLURBS = {
    "Surf": "Web, email, news and messaging apps.",
    "Premium": "Surf plus audio/video messaging and calling (no video streaming).",
    "Stream": "Everything in Premium plus video streaming (Netflix, Disney+ and more).",
}


def parse_beverage_packages(text: str) -> tuple[list[tuple[str, float]], Optional[int]]:
    """[(package, USD per person per day)] and the service-charge % from HAL's beverage page.
    The Have It All package is left out (it's priced from the live fares)."""
    out: list[tuple[str, float]] = []
    for m in re.finditer(r"US ?\$([\d,.]+) Per Person, Per Day\*? ([A-Z][\w ]*?(?:Beverage )?Package)\b", text):
        name = m.group(2).strip()
        if "Have It All" in name or name in {n for n, _ in out}:
            continue
        out.append((name, float(m.group(1).replace(",", ""))))
    for m in re.finditer(r"Starts at US ?\$([\d,.]+) Per Person, Per Day ([A-Z]\w+)", text):
        if m.group(2) not in {n for n, _ in out}:
            out.append((m.group(2), float(m.group(1).replace(",", ""))))
    sc = re.search(r"(\d+)% Service Charge is automatically applied to all Beverage", text, re.I) or \
        re.search(r"(\d+)% service charge", text, re.I)
    return out, int(sc.group(1)) if sc else None


def parse_wifi_plans(text: str) -> list[tuple[str, float]]:
    """[(plan, USD per day)] from HAL's Wi-Fi page ('US$26.00 per day‡ Log In & Upgrade to Premium')."""
    out = []
    for m in re.finditer(r"US ?\$([\d,.]+) per day\S* Log In & Upgrade to (\w+)", text):
        out.append((m.group(2), float(m.group(1).replace(",", ""))))
    return out


def parse_cover_charge(text: str) -> Optional[float]:
    m = re.search(r"(?:US ?)?\$(\d+(?:\.\d+)?) per person", text or "")
    return float(m.group(1)) if m else None


def parse_crew_appreciation(text: str) -> list[tuple[str, float]]:
    m = re.search(r"Crew Appreciation is US ?\$([\d.]+)\*? per guest per day for non-suite stateroom guests "
                  r"and US ?\$([\d.]+)\*? per guest per day for suite guests", text or "")
    if not m:
        return []
    return [("non-suite staterooms", float(m.group(1))), ("suites", float(m.group(2)))]


def excursion_addons(items: list[dict], source: str) -> list[AddOn]:
    """Shore excursions listed with a starting price on each port day of HAL's itinerary."""
    out, seen = [], set()
    for it in items:
        port = re.sub(r"[™®]", "", it.get("title") or it.get("location") or "").strip()
        for act in it.get("onShoreActivities") or []:
            try:
                price = float(str(act.get("startingPrice")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            name = (act.get("activityTitle") or "").strip()
            if not name or (port, name) in seen:
                continue
            seen.add((port, name))
            out.append(AddOn(
                kind="excursion",
                name=name,
                price=price,
                price_unit="per_person",
                unit_label="per adult (from)",
                port=port,
                description="HAL's published starting price; final price is set when booked.",
                source=act.get("activityPagePath") or source,
            ))
    return out


def ship_code_for(ship: str) -> Optional[str]:
    s = re.sub(r"\s+", " ", ship.strip())
    if re.fullmatch(r"[A-Za-z]{2}", s) and s.upper() in SHIP_CODES.values():
        return s.upper()
    key = re.sub(r"^(ms|m/s|hal|holland america)\s+", "", s.lower())
    return SHIP_CODES.get(key)


def cruise_id_from_url(url: str) -> Optional[str]:
    m = URL_RE.search(url or "")
    if m:
        return m.group(2).upper()
    m = URL_PARAM_RE.search(url or "")
    return m.group(1).upper() if m else None


def split_label(value: Optional[str]) -> str:
    """'Zuiderdam#@#UU' → 'Zuiderdam'."""
    return (value or "").split("#@#")[0].strip()


def clock24(t: Optional[str]) -> Optional[str]:
    t = (t or "").strip()
    if not t:
        return None
    for fmt in ("%H:%M", "%I:%M %p"):
        try:
            return datetime.strptime(t, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


def is_hia(price: dict) -> bool:
    """Have It All fares use promo codes N1, N2, ... (NH is the flexible Cruise Only fare)."""
    codes = [str(c).upper() for c in price.get("promoCodes") or []] or [str(price.get("fare") or "").upper()]
    return any(HIA_PROMO.match(c) for c in codes)


def excl_tax(price: dict) -> Optional[float]:
    if price.get("basePrice") is not None:
        return float(price["basePrice"])
    if price.get("price") is None:
        return None
    return float(price["price"]) - float(price.get("tax") or 0)


def class_price(doc: dict, room: str) -> tuple[Optional[float], Optional[float], bool]:
    """(fare excl. tax, tax, offered) from a search doc: Cruise Only lowest, else flexible."""
    offered = False
    for ft in ("RESTRICTED", "BASEPRICE"):
        v = doc.get(f"price_USD_{room}_{ft}_d")
        if v is None or v < 0:
            continue
        offered = True
        if v > 0:
            tax = doc.get(f"taxExpenses_USD_{ft}") or doc.get("taxExpenses_USD_anonymous") or 0.0
            return round(float(v) - float(tax), 2), float(tax), True
    hia = doc.get(f"price_USD_{room}_anonymous_d")
    if hia is not None and hia >= 0:
        offered = True
    return None, None, offered


class HollandAmericaProvider:
    name = "Holland America (live)"
    cruise_line = "Holland America"

    def __init__(self, cf: Optional[CloudflareBrowser] = None, http: Optional[httpx.Client] = None,
                 base_url: str = SITE):
        self.cf = cf
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.base_url = base_url.rstrip("/")
        self._blocked = False
        self.calls = 0

    def handles(self, cruise_line: str) -> bool:
        return "holland" in cruise_line.lower()

    # ── HTTP ─────────────────────────────────────────────────────────────

    @staticmethod
    def _api_headers() -> dict:
        return {"accept": "application/json", "clientid": "WEB", "country": "US", "currencycode": "USD",
                "locale": "en_US", "brand": "hal"}

    def _json(self, path: str, attempts: int = 3):
        url = self.base_url + path
        if not self._blocked:
            for attempt in range(1, attempts + 1):
                self.calls += 1
                try:
                    resp = self.http.get(url, headers={**self._api_headers(), "user-agent": USER_AGENT,
                                                       "referer": self.base_url + LOCALE_PATH + "/find-a-cruise"})
                except httpx.HTTPError as exc:
                    if attempt < attempts:
                        time.sleep(attempt)
                        continue
                    if self.cf is None or not self.cf.configured:
                        raise ProviderError(f"Holland America request {path[:80]} failed: {exc}") from exc
                    log.warning("HAL direct call failed (%s); trying Cloudflare browser", exc)
                    break
                if resp.status_code == 429 or resp.status_code >= 502:
                    if attempt < attempts:
                        time.sleep(float(resp.headers.get("retry-after") or 0) or 2 * attempt)
                        continue
                if resp.status_code in (401, 403, 429) and self.cf is not None and self.cf.configured:
                    log.warning("hollandamerica.com refused %s (%s); switching to Cloudflare browser",
                                path[:80], resp.status_code)
                    self._blocked = True
                    break
                if resp.status_code >= 400:
                    raise ProviderError(f"Holland America request {path[:80]} failed: HTTP {resp.status_code}")
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ProviderError(f"Holland America request {path[:80]} returned non-JSON") from exc
        self.calls += 1
        return fetch_json_in_page(self.cf, self.base_url + LOCALE_PATH + "/find-a-cruise", path, "GET",
                                  self._api_headers(), None, marker="qp-hal",
                                  label=f"Holland America request {path[:80]}")

    def search(self, fq: list[str], start: int = 0, rows: int = 100, sort: str = "departDate asc") -> dict:
        params = [("start", str(start)), ("rows", str(rows)), ("country", "us"), ("language", "en"),
                  ("sort", sort), ("fl", ",".join(SEARCH_FIELDS))] + [("fq", f) for f in fq]
        data = self._json("/search/halcruisesearch?" + urlencode(params))
        return data.get("response") or {}

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        guests = max(1, req.adults + req.children)
        doc = self.find_sailing(req)
        cruise_id = doc["cruiseId"]
        details_url = self.base_url + LOCALE_PATH + (doc.get("contentPath") or "/find-a-cruise")
        sail_date = (doc.get("departDate") or req.sail_date)[:10]

        research = SailingResearch(
            cruise_line=self.cruise_line,
            ship=split_label(doc.get("shipName")) or req.ship,
            sail_date=sail_date,
            nights=doc.get("duration"),
            departure_port=split_label(doc.get("embarkPortName")) or None,
            itinerary_name=(doc.get("name") or "").title() or None,
            itinerary=[],
            currency="USD",
            staterooms=[],
            addons=[],
            sources=[details_url],
            warnings=[],
        )
        warnings = research.warnings

        items: list[dict] = []
        if doc.get("contentPath"):
            try:
                items = self.itinerary_items(doc["contentPath"])
                research.itinerary = self.map_days(items, sail_date)
            except ProviderError as exc:
                warnings.append(f"Couldn't load HAL's day-by-day itinerary ({exc}).")

        try:
            data = (self._json(f"/api/v2/price/cruise/{cruise_id}") or {}).get("data") or {}
            research.staterooms, hia_diff = self.map_staterooms(data, details_url)
        except ProviderError as exc:
            log.warning("HAL room prices for %s failed: %s", cruise_id, exc)
            warnings.append(f"HAL's stateroom prices failed ({exc}); showing search lead-in prices per room type.")
            research.staterooms, hia_diff = [], None
        if not research.staterooms:
            research.staterooms = self.search_rooms(doc, details_url)
        if guests != 2:
            warnings.append("Holland America's online prices are per person for 2 guests; "
                            f"check pricing for {guests} guests with HAL.")

        hia = hia_diff if hia_diff is not None else self.search_hia_diff(doc)
        if hia:
            per_day = f" (about ${hia / research.nights:,.2f} per person per day)" if research.nights else ""
            research.addons.append(AddOn(
                kind="beverage",
                name="Have It All package (fare upgrade)",
                price=hia,
                price_unit="per_person",
                unit_label="per person, whole cruise",
                port=None,
                description=f"{HIA_DESC} Priced as the difference from the Cruise Only fare shown{per_day}.",
                source=details_url,
            ))
        try:
            research.addons += self.fetch_addons(items, details_url, warnings)
        except Exception as exc:  # add-ons must never fail research
            log.warning("HAL add-ons for %s failed: %s", cruise_id, exc)
            warnings.append(f"Couldn't load Holland America add-ons ({exc}).")
        warnings.append(
            "HAL stateroom prices are the 'Cruise Only: Our Lowest Fare' (restricted deposit) where offered; "
            "taxes, fees and port expenses are listed separately."
        )
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def find_sailing(self, req: ResearchRequest) -> dict:
        cruise_id = cruise_id_from_url(req.booking_url)
        if cruise_id:
            docs = self.search([f"cruiseId:{cruise_id}"], rows=20).get("docs") or []
            docs = sorted(docs, key=lambda d: bool(d.get("tourId")))
            if docs:
                return docs[0]
        code = ship_code_for(req.ship)
        if not code:
            raise ProviderError(f"Unknown Holland America ship '{req.ship}'")
        day = f"{req.sail_date}T00:00:00Z"
        docs = self.search([f"shipId:{code}", f"departDate:[{day} TO {day}]"], rows=20).get("docs") or []
        cruise_only = [d for d in docs if not d.get("tourId")]
        if cruise_only:
            return sorted(cruise_only, key=lambda d: (d.get("duration") or 99))[0]
        if docs:
            return docs[0]
        raise ProviderError(f"No Holland America sailing found for {req.ship} ({code}) on {req.sail_date}")

    # ── Itinerary ────────────────────────────────────────────────────────

    def itinerary_items(self, content_path: str) -> list[dict]:
        data = self._json(f"/bin/carnival/hal/us/en{content_path}/itinerarylistview.v2.json")
        return data.get("itineraryListItems") or []

    def fetch_itinerary(self, content_path: str, sail_date: str) -> list[ItineraryDay]:
        return self.map_days(self.itinerary_items(content_path), sail_date)

    # ── Add-ons ──────────────────────────────────────────────────────────

    def _page(self, path: str) -> str:
        """A hollandamerica.com page's text; through Cloudflare if the direct request is refused."""
        url = self.base_url + LOCALE_PATH + path
        err = "blocked"
        if not self._blocked:
            self.calls += 1
            try:
                resp = self.http.get(url, headers={"user-agent": USER_AGENT, "accept": "text/html"})
                if resp.status_code == 200:
                    return page_text(resp.text)
                err = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                err = str(exc)
        if self.cf is None or not self.cf.configured:
            raise ProviderError(f"{url} failed: {err}")
        self.calls += 1
        try:
            return page_text(self.cf.content({"url": url, "gotoOptions": {"waitUntil": "domcontentloaded",
                                                                          "timeout": 30000}}))
        except BrowserRenderingError as exc:
            raise ProviderError(f"{url} failed in browser: {exc}") from exc

    def fetch_addons(self, items: list[dict], source: str, warnings: list[str]) -> list[AddOn]:
        """Beverage packages, Wi-Fi plans, specialty dining and crew appreciation (published prices),
        plus the itinerary's shore excursions."""
        pages = {**PAGES, **{f"dine:{k}": v for k, v in DINING_PAGES.items()}}

        def page(path):
            try:
                return self._page(path)
            except ProviderError as exc:
                log.info("HAL page %s failed: %s", path, exc)
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {k: pool.submit(page, v) for k, v in pages.items()}
            texts = {k: f.result() for k, f in futs.items()}

        def url(path):
            return self.base_url + LOCALE_PATH + path

        out: list[AddOn] = []
        missing: list[str] = []
        bev, sc = parse_beverage_packages(texts["beverage"]) if texts["beverage"] else ([], None)
        sc_note = f"Excludes HAL's {sc or 20}% service charge."
        for name, price in bev:
            out.append(AddOn(kind="beverage", name=name if "Package" in name else f"{name} Beverage Package",
                             price=price, price_unit="per_person_per_day", unit_label="per person, per day",
                             port=None,
                             description=" ".join(x for x in (BEVERAGE_BLURBS.get(name), "Published price"
                                                  + (" (starting at)" if "Package" not in name else "") + ".",
                                                  sc_note, "All adults in the stateroom must buy it.") if x),
                             source=url(PAGES["beverage"])))
        if not bev:
            missing.append("beverage packages")

        wifi = parse_wifi_plans(texts["wifi"]) if texts["wifi"] else []
        for name, price in wifi:
            out.append(AddOn(kind="internet", name=f"{name} Wi-Fi", price=price,
                             price_unit="per_device_per_day", unit_label="per day (one device)", port=None,
                             description=f"{WIFI_BLURBS.get(name, '')} Published price based on a 7-day package; "
                                         "discounts may apply on longer sailings.".strip(),
                             source=url(PAGES["wifi"])))
        if not wifi:
            missing.append("Wi-Fi plans")

        for name, path in DINING_PAGES.items():
            price = parse_cover_charge(texts.get(f"dine:{name}") or "")
            if price is None:
                missing.append(name)
                continue
            out.append(AddOn(kind="dining", name=name, price=price, price_unit="per_person",
                             unit_label="per person, per meal", port=None,
                             description="Specialty restaurant cover charge plus 20% service charge; venues vary by "
                                         "ship. Have It All guests can use their specialty dining credit.",
                             source=url(path)))

        crew = parse_crew_appreciation(texts["crew"]) if texts["crew"] else []
        for label, price in crew:
            out.append(AddOn(kind="other", name=f"Crew appreciation (service charge) - {label}", price=price,
                             price_unit="per_person_per_day", unit_label="per guest, per day", port=None,
                             description="Added daily to the onboard account (adjustable).",
                             source=url(PAGES["crew"])))
        if not crew:
            missing.append("crew appreciation")

        excursions = excursion_addons(items, source)
        out.extend(excursions)
        ex_ports = {a.port for a in excursions}
        no_ex = []
        for it in items[1:]:
            port = re.sub(r"[™®]", "", it.get("title") or "").strip()
            if (it.get("portID") or "").upper() in NON_PORT_IDS or str(it.get("portID") or "").isdigit():
                continue
            if port and port not in ex_ports and port not in no_ex:
                no_ex.append(port)

        if any(a.kind != "excursion" for a in out):
            warnings.append("Holland America drink, Wi-Fi, specialty dining and crew appreciation prices are "
                            "published fleet-wide prices from hollandamerica.com, not quoted for this sailing; "
                            "shore excursion prices are HAL's 'from' prices.")
        if missing:
            warnings.append(f"Couldn't load Holland America add-on prices for: {', '.join(missing)}.")
        if no_ex:
            warnings.append(f"HAL lists no priced shore excursions online for: {', '.join(no_ex)}.")
        return out

    @staticmethod
    def map_days(items: list[dict], sail_date: str) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        out = []
        for i, it in enumerate(items):
            try:
                n = int(str(it.get("dayTitle")).split("-")[0])
            except ValueError:
                n = i + 1
            day_date = None
            for raw in it.get("itineraryDate") or []:
                try:
                    day_date = datetime.strptime(raw.strip(), "%b %d, %Y").date().isoformat()
                    break
                except ValueError:
                    continue
            day_date = day_date or (start + timedelta(days=n - 1)).isoformat()
            sea = (it.get("portID") or "").upper() in NON_PORT_IDS
            port = "At Sea" if sea else re.sub(r"[™®]", "", it.get("title") or it.get("location") or "Port").strip()
            out.append(ItineraryDay(
                day=n,
                date=day_date,
                port=port,
                arrive=None if sea else clock24(it.get("arrivalTime")),
                depart=None if sea else clock24(it.get("departTime")),
            ))
        return out

    # ── Staterooms ───────────────────────────────────────────────────────

    @staticmethod
    def map_staterooms(data: dict, source: str) -> tuple[list[Stateroom], Optional[float]]:
        """One Stateroom per category, at its Cruise Only fare; also the typical HIA upgrade price."""
        rooms: list[tuple[int, float, Stateroom]] = []
        diffs = []
        order = {r: i for i, r in enumerate(ROOM_IDS)}
        for rt in data.get("roomTypes") or []:
            room_id = (rt.get("id") or "").split("_")[-1]
            category = ROOM_CATEGORY.get(room_id, "Other")
            for cat in rt.get("categories") or []:
                # Every listed fare is bookable ("active" is false on sailings the site still sells).
                active = [p for p in cat.get("price") or [] if p.get("price")]
                cruise_only = sorted((p for p in active if not is_hia(p)), key=lambda p: p["price"])
                hia = sorted((p for p in active if is_hia(p)), key=lambda p: p["price"])
                chosen = cruise_only[0] if cruise_only else (hia[0] if hia else None)
                notes = []
                if chosen is not None and not cruise_only:
                    notes.append("Have It All fare (no Cruise Only fare offered)")
                if cruise_only and hia:
                    notes.append(f"Have It All fare ${excl_tax(hia[0]):,.0f} pp")
                    diffs.append(round(hia[0]["price"] - cruise_only[0]["price"], 2))
                if chosen is not None and chosen.get("classification") == "restricted":
                    notes.insert(0, "Cruise Only: lowest fare (restricted)")
                code = (cat.get("id") or "").split("_")[-1] or None
                price = excl_tax(chosen) if chosen else None
                rooms.append((order.get(room_id, 99), price if price is not None else 1e12, Stateroom(
                    category=category,
                    name=f"{cat.get('name') or rt.get('name')} ({code})" if code else (cat.get("name") or ""),
                    code=code,
                    price_per_person=price,
                    taxes_fees_per_person=float(chosen["tax"]) if chosen and chosen.get("tax") is not None else None,
                    sold_out=price is None,
                    notes="; ".join(notes) or ("Sold out" if price is None else None),
                    source=source,
                )))
        rooms.sort(key=lambda r: (r[0], r[1]))
        hia_diff = Counter(diffs).most_common(1)[0][0] if diffs else None
        return [r[2] for r in rooms], hia_diff

    @staticmethod
    def search_rooms(doc: dict, source: str) -> list[Stateroom]:
        """Fallback: the search document's lead fare per room class."""
        names = {"IN": "Inside", "OV": "Ocean View", "VN": "Verandah", "LA": "Lanai", "VS": "Vista Suite",
                 "SS": "Signature Suite", "NS": "Neptune Suite", "PH": "Pinnacle Suite"}
        out = []
        for room in ROOM_IDS:
            price, tax, offered = class_price(doc, room)
            if not offered:
                continue
            out.append(Stateroom(
                category=ROOM_CATEGORY[room],
                name=f"{names[room]} (lowest fare)",
                code=room,
                price_per_person=price,
                taxes_fees_per_person=tax,
                sold_out=price is None,
                notes=None,
                source=source,
            ))
        return out

    @staticmethod
    def search_hia_diff(doc: dict) -> Optional[float]:
        diffs = []
        for room in ROOM_IDS:
            co = doc.get(f"price_USD_{room}_RESTRICTED_d") or 0
            hia = doc.get(f"price_USD_{room}_anonymous_d") or 0
            if co > 0 and hia > co:
                diffs.append(round(hia - co, 2))
        return Counter(diffs).most_common(1)[0][0] if diffs else None

    # ── Master catalog ───────────────────────────────────────────────────

    def iter_catalog(self, page_size: int = 500) -> Iterator[CatalogSailing]:
        """Every bookable cruise (cruisetours excluded) with lead-in class fares: ~3 search calls."""
        start, total, seen = 0, None, set()
        while total is None or start < total:
            resp = self.search(["departDate:[NOW/DAY+1DAY TO *]"], start=start, rows=page_size)
            docs = resp.get("docs") or []
            total = resp.get("numFound") or 0
            if not docs:
                break
            for doc in docs:
                if doc.get("tourId") or doc.get("cruiseId") in seen:
                    continue
                seen.add(doc.get("cruiseId"))
                row = self.catalog_row(doc)
                if row:
                    yield row
            start += page_size

    def catalog_row(self, doc: dict) -> Optional[CatalogSailing]:
        sail_date = (doc.get("departDate") or "")[:10]
        if not doc.get("cruiseId") or not sail_date:
            return None
        prices: dict[str, Optional[float]] = {}
        taxes = None
        for room in ROOM_IDS:
            price, tax, offered = class_price(doc, room)
            if not offered:
                continue
            cls = CATALOG_CLASS[room]
            cur = prices.get(cls)
            if price is not None and (cur is None or price < cur):
                prices[cls] = price
            elif cls not in prices:
                prices[cls] = None
            if tax is not None:
                taxes = tax
        embark = split_label(doc.get("embarkPortName"))
        ports = []
        for label in doc.get("portOfCallIds") or []:
            name, _, pid = label.partition("#@#")
            if pid in NON_PORT_IDS or pid.isdigit() or name == embark or name in ports:
                continue  # sea days, scenic cruising (numeric ids), the embarkation port
            ports.append(name)
        return CatalogSailing(
            cruise_line=self.cruise_line,
            sailing_key=doc["cruiseId"],
            ship=split_label(doc.get("shipName")),
            ship_code=doc.get("shipId"),
            sail_date=sail_date,
            nights=doc.get("duration"),
            itinerary_name=(doc.get("name") or "").title() or None,
            departure_port=embark or None,
            ports=ports,
            booking_url=self.base_url + LOCALE_PATH + doc["contentPath"] if doc.get("contentPath") else None,
            prices=prices,
            taxes_fees_per_person=taxes,
        )
