"""Viking (ocean, expedition and river): live fares from Viking's own "Dates & Pricing" API.

Viking's cruise pages load prices with two JSON endpoints (Pricing_Table_Core_js):
  * POST {root}/Core/DnPCruiseFullInfo  {"cruiseId": <itinerary slug>, ...}
        → every sailing of that itinerary (ship, date, direction, lowest fares, air price)
  * POST {root}/Core/DnPSailingDetails  {"cruiseId", "cruiseName", "sailingKey", "itineraryTcm", ...}
        → per-category fares for one sailing (Veranda (V1), Deluxe Veranda (DV3), ...)

The three product lines share the API but live on different roots:
  ocean       https://www.vikingcruises.com/oceans
  expedition  https://www.vikingcruises.com/expeditions
  river       https://www.vikingrivercruises.com

There is no ship/date search, so the sailing is found by scanning itineraries:
the booking URL's itinerary slug if one was pasted, otherwise the itineraries on
the ship's page first and then everything on the site's search page whose
departure window covers the sail date. The day-by-day itinerary is parsed from
the cruise page for the sailing's direction and year.

Viking's CDN only rejects non-browser User-Agents, so plain httpx with a browser
UA works from server IPs today. If that starts failing (403/network error) and
Cloudflare Browser Rendering is configured, pages are rendered through it and
the JSON endpoints are called from inside a rendered Viking page.

Stateroom categories (Viking name → our category):
  Veranda, Deluxe Veranda, Penthouse Veranda          → Balcony
  Nordic Balcony, Deluxe Nordic Balcony, Nordic Penthouse (expedition) → Balcony
      (a floor-to-ceiling window whose top lowers to an open-air ledge; noted per room)
  River French Balcony, river Veranda                   → Balcony (French Balcony noted: no step-out space)
  River Standard                                        → Ocean View (window, no balcony)
  anything named "... Suite"                            → Suite
Fares are per person, double occupancy, and include port taxes & fees (Viking
Inclusive Value), so taxes_fees_per_person is 0.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest
from .royal_caribbean import parse_price

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Site:
    key: str
    origin: str
    root: str  # path prefix of the site's pages and API ("/oceans", "/expeditions", "")
    label: str

    @property
    def base(self) -> str:
        return self.origin + self.root


SITES = {
    "ocean": Site("ocean", "https://www.vikingcruises.com", "/oceans", "Viking Ocean"),
    "expedition": Site("expedition", "https://www.vikingcruises.com", "/expeditions", "Viking Expeditions"),
    "river": Site("river", "https://www.vikingrivercruises.com", "", "Viking River"),
}

# Names as Viking shows them (shipType), without the "Viking " prefix.
OCEAN_SHIPS = {
    "star", "sea", "sky", "sun", "orion", "jupiter", "venus", "mars", "neptune", "saturn",
    "vela", "vesta", "astrea", "mira", "libra", "lyra", "vega", "leda",
}
EXPEDITION_SHIPS = {"polaris", "octantis"}

API_VERSION = "11"  # Viking's SendAjax appends ?v=11
SCAN_WORKERS = 6
RETRIES = 3
BACKOFF = 1.0  # seconds; doubled per retry, or Retry-After when Viking sends one

# Fields the catalog needs from each DnPCruiseFullInfo sailing (the in-page browser
# script trims to these so a rendered page stays small).
CATALOG_FIELDS = [
    "DepartureDateString", "Ship", "PackageCode", "CruiseName", "sailingKey", "cruiseDirection",
    "cruiseDurationWithYear", "shipType", "soldOut", "stateroomSummary", "lowestPrice", "lowestAirPrice",
]

# DnPCruiseFullInfo stateroomSummary typeName → catalog room class.
SUMMARY_CLASSES = {
    "veranda": "Balcony",
    "french balcony": "Balcony",
    "nordic balcony": "Balcony",
    "suite": "Suite",
    "standard": "Ocean View",
}

BROWSER_HEADERS = {
    "user-agent": USER_AGENT,
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "en-US,en;q=0.9",
}


def ship_key(ship: str) -> str:
    """'Viking Mira' / 'viking mira' / 'Mira' → 'mira'."""
    s = re.sub(r"\s+", " ", (ship or "").strip().lower())
    s = re.sub(r"^(the )?viking ", "", s)
    return s


def sites_for(ship: str, booking_url: str = "") -> list[Site]:
    url = (booking_url or "").lower()
    if "vikingrivercruises.com" in url:
        return [SITES["river"]]
    if "/expeditions/" in url:
        return [SITES["expedition"]]
    if "/oceans/" in url:
        return [SITES["ocean"]]
    key = ship_key(ship)
    if key in EXPEDITION_SHIPS:
        return [SITES["expedition"], SITES["ocean"], SITES["river"]]
    if key in OCEAN_SHIPS:
        return [SITES["ocean"], SITES["expedition"], SITES["river"]]
    return [SITES["river"], SITES["ocean"], SITES["expedition"]]


def slug_from_url(booking_url: str) -> Optional[str]:
    """The itinerary slug (Viking's cruiseId) from a Viking cruise page URL.

    /oceans/cruise-destinations/western-mediterranean/iconic-western-mediterranean/pricing.html
    /cruise-destinations/europe/rhine-getaway/2027-basel-amsterdam/index.html
    """
    if not booking_url or "viking" not in booking_url.lower():
        return None
    parts = [p for p in urlparse(booking_url).path.split("/") if p]
    if "cruise-destinations" not in parts:
        return None
    for seg in reversed(parts):
        if seg.endswith(".html") or re.match(r"\d{4}-", seg):
            continue
        return seg if re.fullmatch(r"[a-z0-9-]+", seg) and seg != "cruise-destinations" else None
    return None


def category_for(name: str) -> tuple[str, Optional[str]]:
    """Our stateroom category, plus a note for the less obvious mappings."""
    n = name.lower()
    if "suite" in n:
        return "Suite", None
    if "french balcony" in n:
        return "Balcony", "French balcony: floor-to-ceiling sliding door, no step-out space"
    if "nordic" in n:
        return "Balcony", "Nordic Balcony: floor-to-ceiling window that lowers to an open-air ledge"
    if "veranda" in n or "balcony" in n or "penthouse" in n:
        return "Balcony", None
    if "standard" in n or "window" in n or "ocean view" in n:
        return "Ocean View", None
    if "interior" in n or "inside" in n:
        return "Interior", None
    return "Other", None


def split_suite_name(suite_name: str) -> tuple[str, Optional[str]]:
    """'Deluxe Veranda (DV3)' → ('Deluxe Veranda', 'DV3')."""
    m = re.match(r"^(.*?)\s*\(([A-Z0-9]{1,5})\)\s*$", suite_name or "")
    if m:
        return m.group(1).strip(), m.group(2)
    return (suite_name or "").strip(), None


def extract_view_model(html: str) -> Optional[dict]:
    """The search page embeds every itinerary in window.app.superfac.viewModel."""
    marker = "window.app.superfac.viewModel"
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[j:])
    except ValueError:
        return None
    return obj


def departures_cover(departures: str, sail: date) -> bool:
    """'Sep 2026 - Aug 2029' covers 2027-03-14? Unknown formats count as covering."""
    m = re.findall(r"([A-Za-z]{3})\w*\s+(\d{4})", departures or "")
    if len(m) < 2:
        return True
    try:
        start = datetime.strptime(f"{m[0][0]} {m[0][1]}", "%b %Y").date()
        end = datetime.strptime(f"{m[-1][0]} {m[-1][1]}", "%b %Y").date()
    except ValueError:
        return True
    return (start.year, start.month) <= (sail.year, sail.month) <= (end.year, end.month)


def nights_between(sail_date: str, duration_with_year: str) -> Optional[int]:
    """'Oct 14 - Oct 21, 2026' → 7 (handles a year boundary)."""
    m = re.search(r"-\s*([A-Za-z]{3})\w*\s+(\d{1,2}),\s*(\d{4})", duration_with_year or "")
    if not m:
        return None
    try:
        start = date.fromisoformat(sail_date)
        end = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    except ValueError:
        return None
    while end < start:
        end = end.replace(year=end.year + 1)
    while (end - start).days > 400:
        end = end.replace(year=end.year - 1)
    return (end - start).days


def sailing_date(entry: dict) -> Optional[str]:
    raw = entry.get("DepartureDateString") or ""
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return None


class VikingProvider:
    name = "Viking (live)"

    def __init__(self, cf: CloudflareBrowser, http: Optional[httpx.Client] = None):
        self.cf = cf
        self.http = http or httpx.Client(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
        # Flipped once direct requests are refused, so later calls go straight to the browser.
        self._use_browser = False

    cruise_line = "Viking"
    # Every Viking fare includes Wi-Fi and all dining (specialty restaurants too), so web
    # research shouldn't go looking for paid packages of those kinds.
    included_addon_kinds = ("internet", "dining")

    def handles(self, cruise_line: str) -> bool:
        return "viking" in cruise_line.lower()

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        if not ship_key(req.ship):
            raise ProviderError("Enter the Viking ship name (e.g. Viking Mira)")
        try:
            date.fromisoformat(req.sail_date)
        except ValueError as exc:
            raise ProviderError(f"Bad sail date '{req.sail_date}'") from exc

        found = self.find_sailing(req)
        if not found:
            raise ProviderError(f"No Viking sailing found for {req.ship} on {req.sail_date}")
        site, slug, page_url, entry, offer = found

        research = SailingResearch(
            cruise_line="Viking",
            ship=entry.get("shipType") or req.ship,
            sail_date=req.sail_date,
            nights=nights_between(req.sail_date, entry.get("cruiseDurationWithYear") or ""),
            departure_port=(entry.get("cruiseDirection") or "").split(" to ")[0] or None,
            itinerary_name=entry.get("CruiseName"),
            itinerary=[],
            currency="USD",
            staterooms=[],
            addons=[],
            sources=[self._pricing_url(site, page_url)],
            warnings=[],
        )
        warnings = research.warnings
        if entry.get("soldOut"):
            warnings.append("Viking shows this sailing as sold out.")

        try:
            research.staterooms = self.fetch_staterooms(site, slug, entry, offer, research.sources[0])
            if not research.staterooms:
                warnings.append("Viking returned no stateroom categories for this sailing.")
        except ProviderError as exc:
            log.warning("Viking stateroom fetch failed: %s", exc)
            warnings.append(f"Stateroom prices could not be loaded from Viking: {exc}")

        itin_html, itin_url = "", ""
        if page_url:
            try:
                research.itinerary, itin_url, itin_html = self.fetch_itinerary(
                    site, page_url, entry.get("cruiseDirection") or "", req.sail_date
                )
                if research.itinerary and itin_url not in research.sources:
                    research.sources.append(itin_url)
            except ProviderError as exc:
                log.warning("Viking itinerary fetch failed: %s", exc)
                warnings.append(f"Day-by-day itinerary could not be loaded from Viking: {exc}")

        try:
            research.addons = self.build_addons(site, entry, warnings, itin_html, itin_url, research.itinerary)
        except Exception as exc:  # add-ons must never fail the research
            log.exception("Viking add-ons failed")
            warnings.append(f"Viking add-ons could not be loaded: {exc}")
        if req.children:
            warnings.append("Viking sails adults-only (18+); children can't be booked on this sailing.")
        return research

    # ── HTTP (direct, falling back to Cloudflare Browser Rendering) ──────

    def get_html(self, url: str) -> str:
        if not self._use_browser:
            try:
                resp = self.http.get(url, headers={"accept": "text/html,application/xhtml+xml"})
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 404:
                    raise ProviderError(f"Viking page not found: {url}")
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last = str(exc)
            if not self.cf.configured:
                raise ProviderError(f"Viking blocked or failed ({last}) for {url}")
            self._use_browser = True
        try:
            return self.cf.content({"url": url, "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000}})
        except BrowserRenderingError as exc:
            raise ProviderError(f"Viking page render failed: {exc}") from exc

    def post_many(self, site: Site, path: str, bodies: list[dict]) -> list[Optional[dict]]:
        """POST each body to a Viking JSON endpoint; results in the same order (None on failure)."""
        if not bodies:
            return []
        if not self._use_browser:
            url = f"{site.base}{path}?v={API_VERSION}"
            blocked = []

            def one(body):
                payload = json.dumps({k: v for k, v in body.items() if not k.startswith("__")})
                for attempt in range(RETRIES):
                    try:
                        resp = self.http.post(
                            url,
                            content=payload,
                            headers={
                                "content-type": "application/json; charset=utf-8",
                                "x-requested-with": "XMLHttpRequest",
                                "origin": site.origin,
                                "referer": site.base + "/",
                            },
                        )
                    except httpx.HTTPError as exc:
                        if attempt == RETRIES - 1:
                            blocked.append(str(exc))
                            return None
                        self._sleep(BACKOFF * 2**attempt)
                        continue
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if attempt == RETRIES - 1:
                            if resp.status_code == 429:
                                blocked.append("HTTP 429")
                            return None
                        retry_after = resp.headers.get("retry-after", "")
                        wait = float(retry_after) if retry_after.isdigit() else BACKOFF * 2**attempt
                        self._sleep(min(wait, 30))
                        continue
                    if resp.status_code in (401, 403):
                        blocked.append(f"HTTP {resp.status_code}")
                        return None
                    try:
                        return resp.json() if resp.status_code == 200 else None
                    except ValueError:
                        return None
                return None

            if len(bodies) == 1:
                out = [one(bodies[0])]
            else:
                with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                    out = list(pool.map(one, bodies))
            if not blocked or any(o is not None for o in out):
                return out
            if not self.cf.configured:
                raise ProviderError(
                    f"Viking blocked the request ({blocked[0]}). Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN."
                )
            self._use_browser = True
        return self._post_via_browser(site, path, bodies)

    def _post_via_browser(self, site: Site, path: str, bodies: list[dict]) -> list[Optional[dict]]:
        # Load a light Viking page (gets the CDN's cookies) and make the calls from inside it.
        # Full cruise lists are trimmed in-page to sailings on the wanted date to keep the HTML small.
        script = (
            "(async function(){var bodies=" + json.dumps(bodies) + ";var out=[];"
            "for(var i=0;i<bodies.length;i++){var r=null;try{r=await fetch('" + site.root + path
            + "?v=" + API_VERSION + "',{method:'POST',headers:{'content-type':'application/json; charset=utf-8',"
            "'x-requested-with':'XMLHttpRequest'},body:JSON.stringify(bodies[i])}).then(function(x){return x.json();});"
            "if(r&&r.cruises&&bodies[i].__date){r.cruises=r.cruises.filter(function(c){"
            "return c.DepartureDateString===bodies[i].__date;});r.calendar=null;}"
            "if(r&&r.cruises&&bodies[i].__slim){var keep=bodies[i].__slim;r.calendar=null;"
            "r.cruises=r.cruises.map(function(c){var o={};keep.forEach(function(k){o[k]=c[k];});return o;});}"
            "}catch(e){r=null;}out.push(r);}"
            "var d=document.createElement('div');d.id='qp-viking';"
            "d.setAttribute('data-json',encodeURIComponent(JSON.stringify(out)));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content(
                {
                    "url": f"{site.base}/frequently-asked-questions.html",
                    "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                    "addScriptTag": [{"content": script}],
                    "waitForSelector": {"selector": "#qp-viking", "timeout": 55000},
                }
            )
        except BrowserRenderingError as exc:
            raise ProviderError(f"Viking pricing failed in browser: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="qp-viking")
        if node is None:
            raise ProviderError("Viking pricing returned no data from the browser")
        try:
            out = json.loads(unquote(node.get("data-json", "")))
        except ValueError as exc:
            raise ProviderError("Viking pricing returned unreadable data") from exc
        return out if isinstance(out, list) else [None] * len(bodies)

    # ── Sailing lookup ───────────────────────────────────────────────────

    def find_sailing(self, req: ResearchRequest):
        """→ (site, slug, cruise page URL, sailing entry, offer code) or None."""
        want_ship = ship_key(req.ship)
        want_date = req.sail_date.replace("-", "")
        sail = date.fromisoformat(req.sail_date)

        url_slug = slug_from_url(req.booking_url)
        for site in sites_for(req.ship, req.booking_url):
            if url_slug:
                page = req.booking_url.split("?")[0].split("#")[0].replace("pricing.html", "index.html")
                hit = self._scan(site, [(url_slug, page)], want_ship, want_date, "")
                if hit:
                    return hit

            try:
                results, offer = self.search_results(site)
            except ProviderError as exc:
                log.warning("Viking %s search failed: %s", site.key, exc)
                continue
            candidates = [
                (r["TcmId"], urljoin(site.origin, r.get("PageUrl") or ""))
                for r in results
                if r.get("TcmId") and departures_cover(r.get("Departures") or "", sail)
            ]
            seen = set()
            candidates = [c for c in candidates if not (c[0] in seen or seen.add(c[0]))]
            if url_slug:
                candidates = [c for c in candidates if c[0] != url_slug]

            preferred = self.ship_itineraries(site, want_ship)
            first = [c for c in candidates if c[0] in preferred]
            rest = [c for c in candidates if c[0] not in preferred]
            for group in (first, rest):
                hit = self._scan(site, group, want_ship, want_date, offer)
                if hit:
                    return hit
        return None

    def _scan(self, site: Site, candidates: list[tuple[str, str]], want_ship: str, want_date: str, offer: str):
        batch = SCAN_WORKERS * 2
        for i in range(0, len(candidates), batch):
            chunk = candidates[i : i + batch]
            bodies = [
                {"cruiseId": slug, "parameters": "", "offerCode": offer, "__date": want_date}
                for slug, _ in chunk
            ]
            # __date is only read by the in-page browser script (never sent to Viking directly).
            results = self.post_many(site, "/Core/DnPCruiseFullInfo", bodies)
            for (slug, page), data in zip(chunk, results):
                entry = self.pick_sailing((data or {}).get("cruises") or [], want_ship, want_date)
                if entry:
                    return site, slug, page, entry, (data or {}).get("offerCode") or offer
        return None

    @staticmethod
    def pick_sailing(cruises: list[dict], want_ship: str, want_date: str) -> Optional[dict]:
        for c in cruises:
            if (c.get("DepartureDateString") or "") != want_date:
                continue
            if ship_key(c.get("shipType") or "") == want_ship:
                return c
        return None

    def search_results(self, site: Site) -> tuple[list[dict], str]:
        html = self.get_html(f"{site.base}/search-cruises/index.html")
        vm = extract_view_model(html)
        if not vm:
            raise ProviderError("Viking search page had no cruise list")
        value = ((vm.get("PageData") or {}).get("Value")) or {}
        return value.get("Results") or [], value.get("CurrentPromoCode") or ""

    def ship_itineraries(self, site: Site, want_ship: str) -> set[str]:
        """Itinerary slugs linked from the ship's page (ocean/expedition only) — scanned first."""
        if site.key == "river" or not re.fullmatch(r"[a-z ]+", want_ship):
            return set()
        try:
            html = self.get_html(f"{site.base}/ships/viking-{want_ship.replace(' ', '-')}.html")
        except ProviderError:
            return set()
        return set(re.findall(r"/cruise-destinations/[a-z0-9-]+/([a-z0-9-]+)/(?:index|pricing)\.html", html))

    # ── Staterooms ───────────────────────────────────────────────────────

    @staticmethod
    def _pricing_url(site: Site, page_url: str) -> str:
        if page_url:
            return page_url.split("?")[0].replace("index.html", "pricing.html")
        return f"{site.base}/search-cruises/index.html"

    def fetch_staterooms(self, site: Site, slug: str, entry: dict, offer: str, source: str) -> list[Stateroom]:
        body = {
            "cruiseId": slug,
            "cruiseName": entry.get("CruiseName") or "",
            "sailingKey": entry.get("sailingKey") or "",
            "itineraryTcm": entry.get("itineraryTcm") or "",
            "offerCode": offer or "",
            "parameters": "",
        }
        data = self.post_many(site, "/Core/DnPSailingDetails", [body])[0]
        if data is None:
            raise ProviderError("sailing details request failed")
        suites = ((data.get("sailingData") or {}).get("cruiseSuites")) or []
        return self.map_suites(suites, source)

    @staticmethod
    def map_suites(suites: list[dict], source: str) -> list[Stateroom]:
        rooms = []
        for s in suites:
            name, code = split_suite_name(s.get("suiteName") or "")
            if not name:
                continue
            category, note = category_for(name)
            sold_out = bool(s.get("soldOut"))
            price = parse_price(s.get("discountedPrice")) if s.get("hasPricing", True) else None
            was = parse_price(s.get("originalPrice"))
            notes = []
            if was and price and was > price:
                notes.append(f"Standard fare ${was:,.0f}")
            if s.get("stateroomDeal"):
                notes.append(s["stateroomDeal"])
            if s.get("availLabel") and not sold_out:
                notes.append(s["availLabel"])
            if sold_out and price:
                notes.append(f"Sold out (last fare ${price:,.0f})")
            if s.get("stateroomSize"):
                notes.append(f"{s['stateroomSize']} sq ft")
            if note:
                notes.append(note)
            notes.append("Port taxes & fees included")
            rooms.append(
                Stateroom(
                    category=category,
                    # Keep Viking's "(DV3)" suffix: several price tiers share a name, and
                    # selections are stored by category|name.
                    name=f"{name} ({code})" if code else name,
                    code=code,
                    price_per_person=None if sold_out else price,
                    taxes_fees_per_person=0.0,
                    sold_out=sold_out or price is None,
                    notes="; ".join(notes),
                    source=source,
                )
            )
        return rooms

    # ── Itinerary ────────────────────────────────────────────────────────

    def fetch_itinerary(
        self, site: Site, page_url: str, direction: str, sail_date: str
    ) -> tuple[list[ItineraryDay], str, str]:
        """Day-by-day from the cruise page for this sailing's direction and year → (days, url, html)."""
        year = sail_date[:4]
        html = self.get_html(page_url)
        url = page_url
        target = self.variant_url(html, direction, year)
        if target:
            target = urljoin(site.origin, target)
            if target != url:
                try:
                    html, url = self.get_html(target), target
                except ProviderError:
                    pass  # keep the base page's days (right itinerary, maybe another direction/year)
        days = self.parse_days(html, sail_date)
        if not days:
            raise ProviderError("no day-by-day itinerary on the cruise page")
        return days, url, html

    @staticmethod
    def variant_url(html: str, direction: str, year: str) -> Optional[str]:
        """The cruise-page link for `direction` in `year` (Viking has one page per direction/year)."""
        soup = BeautifulSoup(html, "html.parser")
        norm = lambda s: re.sub(r"\W+", " ", s or "").strip().lower()
        want = norm(direction)
        dir_href = None
        for a in soup.select("a[aria-label^='Change the cruise direction']"):
            label = a.get("aria-label", "").split(" - ", 1)[-1].rstrip(".")
            if norm(label) == want and a.get("href"):
                dir_href = a["href"]
                break
        if dir_href is None:
            # No direction switcher (one-way itinerary) or unknown direction: pick the year link.
            for a in soup.select("a[aria-label^='Change the cruise year']"):
                if a.get("aria-label", "").rstrip(".").endswith(year) and a.get("href"):
                    return a["href"]
            return None
        # Ocean/expedition pages carry the year in ?year=, river pages in the path (/2027-basel-amsterdam/).
        if re.search(r"year=\d{4}", dir_href):
            return re.sub(r"year=\d{4}", f"year={year}", dir_href)
        if re.search(r"/\d{4}-", dir_href):
            return re.sub(r"/\d{4}-", f"/{year}-", dir_href, count=1)
        return dir_href

    @staticmethod
    def parse_days(html: str, sail_date: str) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        soup = BeautifulSoup(html, "html.parser")
        out: list[ItineraryDay] = []
        for row in soup.select(".day-row"):
            num_el = row.select_one(".day-number")
            city_el = row.select_one(".row-city .city-label") or row.select_one(".row-city")
            port = city_el.get_text(" ", strip=True) if city_el else ""
            if not port:
                continue
            if re.fullmatch(r"(day )?at sea", port, re.I):
                port = "At Sea"
            nums = re.findall(r"\d+", num_el.get_text(" ", strip=True) if num_el else "")
            if not nums:
                # A second stop on the same day (river pages list e.g. Speyer, then Rüdesheim).
                if out:
                    out[-1].port = f"{out[-1].port} / {port}"
                continue
            first, last = int(nums[0]), int(nums[-1])
            if last < first or last - first > 30:
                last = first
            for n in range(first, last + 1):
                out.append(
                    ItineraryDay(
                        day=n,
                        date=(start + timedelta(days=n - 1)).isoformat(),
                        port=port,
                        arrive=None,
                        depart=None,
                    )
                )
        return out

    # ── Add-ons ──────────────────────────────────────────────────────────
    #
    # What Viking sells beyond the fare, from Viking's own public pages:
    #   * Silver Spirits Beverage Package — /my-trip/silver-spirits-beverage-package page
    #   * crew gratuities and the Viking Air Plus fee — the site's FAQ
    #   * Viking Air — the sailing's own lowest air add-on (DnPCruiseFullInfo)
    #   * pre/post cruise extensions — "from" prices in the cruise page's JSON-LD
    #   * shore excursions per port — each itinerary day page ("?itineraryday=N").
    #     Viking marks the included one(s) but only shows optional excursion prices in
    #     My Viking Journey after booking, so optional ones are listed without a price.
    # Wi-Fi and specialty dining are included in every fare (see `included_addon_kinds`).

    FARE_INCLUDES = (
        "Fare already includes one shore excursion in every port, Wi-Fi, all onboard dining "
        "(specialty restaurants too), beer, wine & soft drinks with lunch and dinner, "
        "specialty coffees & teas, and port taxes & fees."
    )

    def build_addons(
        self,
        site: Site,
        entry: dict,
        warnings: list[str],
        itinerary_html: str = "",
        itinerary_url: str = "",
        days: Optional[list[ItineraryDay]] = None,
    ) -> list[AddOn]:
        ss_url = f"{site.base}/my-trip/silver-spirits-beverage-package/index.html"
        faq_url = f"{site.base}/frequently-asked-questions.html"
        day_urls = self.day_page_urls(site, itinerary_html, itinerary_url) if itinerary_html else {}

        def fetch(url):
            try:
                return self.get_html(url)
            except Exception as exc:  # one missing page must not sink the rest
                log.warning("Viking add-on page failed %s: %s", url, exc)
                return None

        urls = [ss_url, faq_url, *day_urls.values()]
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            pages = dict(zip(urls, pool.map(fetch, urls)))

        addons: list[AddOn] = []
        faq = self.parse_faq(pages.get(faq_url) or "")

        # Beverage
        price = self.parse_silver_spirits(pages.get(ss_url) or "") or faq.get("silver_spirits")
        if price is not None:
            addons.append(
                AddOn(
                    kind="beverage",
                    name="Silver Spirits Beverage Package",
                    price=price,
                    price_unit="per_person_per_day",
                    unit_label="per guest, per night (both guests in a stateroom, whole voyage)",
                    port=None,
                    description=(
                        "Premium wines, spirits, cocktails and specialty coffees all day in every venue. "
                        "No separate service charge. " + self.FARE_INCLUDES
                    ),
                    source=ss_url,
                )
            )
        else:
            warnings.append("Couldn't read the Silver Spirits Beverage Package price from Viking.")

        # Gratuities and other fees
        if faq.get("gratuity") is not None:
            addons.append(
                AddOn(
                    kind="other",
                    name="Crew gratuities",
                    price=faq["gratuity"],
                    price_unit="per_person_per_day",
                    unit_label="per person, per night",
                    port=None,
                    description=(
                        "Viking's recommended rate; pre-pay in My Viking Journey or it is added to the "
                        "onboard account automatically (adjustable at Guest Services)."
                        + (" River rates vary by destination; this is the Europe rate." if site.key == "river" else "")
                    ),
                    source=faq_url,
                )
            )
        air = entry.get("lowestAirPrice")
        if isinstance(air, (int, float)) and air > 0:
            addons.append(
                AddOn(
                    kind="other",
                    name="Viking Air (round-trip flights + transfers)",
                    price=float(air),
                    price_unit="per_person",
                    unit_label="per person, from select US gateways",
                    port=None,
                    description=(
                        "Lowest Viking Air add-on for this sailing, including airport transfers and air taxes"
                        + (f" (advertised {entry['advertisedAirFare']})." if entry.get("advertisedAirFare") else ".")
                    ),
                    source="Viking Dates & Pricing",
                )
            )
        if faq.get("air_plus") is not None:
            addons.append(
                AddOn(
                    kind="other",
                    name="Viking Air Plus (custom flights service fee)",
                    price=faq["air_plus"],
                    price_unit="per_person",
                    unit_label="per guest, non-refundable",
                    port=None,
                    description="Fee to have a Viking Air Expert customize flights (plus any fare difference).",
                    source=faq_url,
                )
            )

        # Pre/post extensions
        extensions = self.parse_extensions(itinerary_html, itinerary_url) if itinerary_html else []
        addons += extensions
        if extensions:
            warnings.append(
                "Viking pre/post extension prices are published 'from' prices per person, not quoted for this date."
            )

        # Shore excursions
        excursions: list[AddOn] = []
        seen = set()
        ports_by_day = {d.day: d.port for d in days or []}
        for n, url in day_urls.items():
            for x in self.parse_excursions(pages.get(url) or "", ports_by_day.get(n), url):
                if (x.port, x.name) not in seen:
                    seen.add((x.port, x.name))
                    excursions.append(x)
        addons += excursions
        optional = sum(1 for x in excursions if x.price is None)
        if optional:
            warnings.append(
                f"Viking doesn't publish optional shore excursion prices before booking; {optional} optional "
                "excursions are listed without a price (see My Viking Journey once booked)."
            )
        elif not excursions:
            warnings.append(
                "Couldn't read Viking's shore excursions for this sailing; one excursion per port is included in the fare."
            )
        return addons

    def silver_spirits_price(self, site: Site) -> Optional[float]:
        try:
            html = self.get_html(f"{site.base}/my-trip/silver-spirits-beverage-package/index.html")
        except ProviderError:
            return None
        return self.parse_silver_spirits(html)

    @staticmethod
    def parse_silver_spirits(html: str) -> Optional[float]:
        text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
        m = re.search(r"Silver Spirits Beverage Package\s*\(\s*\$(\d+(?:\.\d+)?)\s*USD\s*/\s*Day", text, re.I) or re.search(
            r"\$(\d+(?:\.\d+)?)\s*USD\s*(?:per|/)\s*(?:day|night)", text, re.I
        )
        return float(m.group(1)) if m else None

    @staticmethod
    def parse_faq(html: str) -> dict[str, float]:
        """Fees from the FAQ's embedded answers: gratuity rate, Silver Spirits, Air Plus fee."""
        out: dict[str, float] = {}
        text = re.sub(r"<[^>]+>|\\u003c[^\\]*?\\u003e", " ", html or "")
        text = re.sub(r"\s+", " ", text.replace("&nbsp;", " ").replace("\\u0026nbsp;", " "))
        patterns = {
            "gratuity": r"(?:recommended gratuity rate of|recommended rate is) \$(\d+(?:\.\d+)?) USD per person, per night",
            "silver_spirits": r"Silver Spirits Beverage Package\. For only \$(\d+(?:\.\d+)?) USD per night",
            "air_plus": r"Air Plus service fee of \$(\d+(?:\.\d+)?) USD per guest",
        }
        for key, pat in patterns.items():
            m = re.search(pat, text)
            if m:
                out[key] = float(m.group(1))
        return out

    @staticmethod
    def parse_extensions(html: str, source: str) -> list[AddOn]:
        """Pre/post cruise extensions from the cruise page's schema.org TouristTrip products."""
        out = []
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html or "", re.S):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            name = (data.get("name") or "") if isinstance(data, dict) else ""
            if not re.match(r"(Pre|Post):", name):
                continue
            offer = data.get("offers") or {}
            price = parse_price(str(offer.get("lowPrice") or "")) if isinstance(offer, dict) else None
            nights = re.fullmatch(r"P(\d+)D", data.get("duration") or "")
            desc = BeautifulSoup(data.get("description") or "", "html.parser").get_text(" ", strip=True)
            lead = f"{'Pre' if name.startswith('Pre') else 'Post'}-cruise extension"
            if nights:
                lead += f", {nights.group(1)} nights"
            out.append(
                AddOn(
                    kind="other",
                    name=f"{name.split(':', 1)[0]}-cruise extension: {name.split(':', 1)[1].strip()}",
                    price=price,
                    price_unit="per_person",
                    unit_label="per person, double occupancy (from)",
                    port=None,
                    description=f"{lead}; published 'from' price. {desc}".strip()[:500],
                    source=source,
                )
            )
        return out

    @staticmethod
    def day_page_urls(site: Site, html: str, page_url: str) -> dict[int, str]:
        """Itinerary day number → its day page, keeping the page's direction/year query."""
        query = urlparse(page_url).query if page_url else ""
        query = "&".join(q for q in query.split("&") if q and not q.startswith("itineraryday="))
        out: dict[int, str] = {}
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select(".day-row a[href*='itineraryday=']"):
            m = re.search(r"itineraryday=(\d+)", a["href"])
            if not m or int(m.group(1)) in out:
                continue
            path = a["href"].split("?")[0]
            q = f"{query}&itineraryday={m.group(1)}" if query else f"itineraryday={m.group(1)}"
            out[int(m.group(1))] = urljoin(site.origin, f"{path}?{q}")
        return out

    @staticmethod
    def parse_excursions(html: str, day_port: Optional[str], source: str) -> list[AddOn]:
        """Shore excursion tiles on an itinerary day page (Included badge = in the fare)."""
        soup = BeautifulSoup(html or "", "html.parser")
        tiles = soup.select("a.dynamicModal[data-template-name='ExcursionDetail']")
        # Multi-stop days ("Speyer / Rüdesheim"): Viking's item ids start with a per-port
        # code (despe…, derue…), in the same order as the stops.
        stops = [
            p.strip()
            for p in (day_port or "").split(" / ")
            if p.strip() and not re.match(r"(scenic sailing|sail |cruising|at sea|explore )", p.strip(), re.I)
        ]
        prefixes: list[str] = []
        for t in tiles:
            pre = (t.get("data-item-id") or "")[:5]
            if pre and pre not in prefixes:
                prefixes.append(pre)
        out = []
        for t in tiles:
            title = t.select_one("h3")
            name = title.get_text(" ", strip=True) if title else (t.get("data-template-id") or "")
            if not name:
                continue
            badge = t.select_one(".badge-top-left")
            included = bool(badge and "included" in badge.get_text(" ", strip=True).lower())
            sub = t.select_one(".subtitle")
            pre = (t.get("data-item-id") or "")[:5]
            if len(stops) > 1 and len(prefixes) == len(stops) and pre in prefixes:
                port = stops[prefixes.index(pre)]
            else:
                port = day_port
            note = (
                "Included in the fare."
                if included
                else "Optional excursion; Viking shows its price in My Viking Journey after booking."
            )
            out.append(
                AddOn(
                    kind="excursion",
                    name=name,
                    price=0.0 if included else None,
                    price_unit="per_person",
                    unit_label="per person",
                    port=port,
                    description=f"{sub.get_text(' ', strip=True) + ' ' if sub else ''}{note}",
                    source=source,
                )
            )
        return out

    # ── Bulk catalog ─────────────────────────────────────────────────────

    def iter_catalog(self, today: Optional[date] = None) -> Iterator[CatalogSailing]:
        """Every future Viking sailing (ocean, expedition, river) with lead-in fares per class.

        One search page per product line (it embeds every itinerary slug), then one
        DnPCruiseFullInfo call per itinerary, which lists all of its sailings with the
        cheapest available fare per stateroom type. About 190 calls for the whole line.
        """
        cutoff = (today or date.today()).strftime("%Y%m%d")
        seen: set[str] = set()
        for site in SITES.values():
            try:
                results, offer = self.search_results(site)
            except ProviderError as exc:
                log.warning("Viking %s catalog search failed: %s", site.key, exc)
                continue
            itineraries: dict[str, dict] = {}
            for r in results:
                if r.get("TcmId"):
                    itineraries.setdefault(r["TcmId"], r)
            slugs = list(itineraries)
            for i in range(0, len(slugs), SCAN_WORKERS):
                chunk = slugs[i : i + SCAN_WORKERS]
                bodies = [
                    {"cruiseId": slug, "parameters": "", "offerCode": offer, "__slim": CATALOG_FIELDS}
                    for slug in chunk
                ]
                try:
                    datas = self.post_many(site, "/Core/DnPCruiseFullInfo", bodies)
                except ProviderError as exc:
                    log.warning("Viking %s catalog stopped: %s", site.key, exc)
                    break
                for slug, data in zip(chunk, datas):
                    if not data:
                        log.warning("Viking %s: no sailings for %s", site.key, slug)
                        continue
                    for row in self.catalog_rows(site, itineraries[slug], data, cutoff):
                        if row.sailing_key not in seen:
                            seen.add(row.sailing_key)
                            yield row

    @staticmethod
    def catalog_rows(site: Site, result: dict, data: dict, cutoff: str) -> list[CatalogSailing]:
        """CatalogSailings from one DnPCruiseFullInfo response (sailings on/after `cutoff`)."""
        classes = [SUMMARY_CLASSES.get(t, category_for(t)[0]) for t in data.get("suiteTypes") or []]
        page = urljoin(site.origin, (result.get("PageUrl") or "").split("?")[0])
        pricing = page.replace("index.html", "pricing.html") if page.endswith("index.html") else page
        cities = [c for c in result.get("Cities") or [] if c]
        rows = []
        for c in data.get("cruises") or []:
            day = c.get("DepartureDateString") or ""
            code = c.get("PackageCode")
            if not code or not re.fullmatch(r"\d{8}", day) or day < cutoff:
                continue
            sail_date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            direction = c.get("cruiseDirection") or result.get("Direction") or ""
            ports = list(cities)
            if ports and direction and direction != result.get("Direction"):
                a, _, b = (result.get("Direction") or "").partition(" to ")
                if direction == f"{b} to {a}":
                    ports.reverse()
            rows.append(
                CatalogSailing(
                    cruise_line="Viking",
                    sailing_key=code,
                    ship=c.get("shipType") or "",
                    ship_code=c.get("Ship"),
                    sail_date=sail_date,
                    nights=nights_between(sail_date, c.get("cruiseDurationWithYear") or ""),
                    itinerary_name=c.get("CruiseName") or result.get("CruiseName"),
                    departure_port=direction.split(" to ")[0] or None,
                    ports=ports,
                    booking_url=f"{pricing}?voyageId={code}" if pricing else None,
                    prices=VikingProvider.class_prices(site, c, classes),
                    taxes_fees_per_person=0.0,  # Viking fares include port taxes & fees
                    currency="USD",
                )
            )
        return rows

    @staticmethod
    def class_prices(site: Site, sailing: dict, classes: list[str]) -> dict[str, Optional[float]]:
        """Lead-in fare per class; classes Viking sells on the itinerary but not here are None (sold out)."""
        summary = sailing.get("stateroomSummary") or []
        if not summary and not sailing.get("soldOut"):
            # Far-out sailings carry only a "from" fare, no per-class breakdown.
            prices: dict[str, Optional[float]] = {}
        else:
            prices = {cls: None for cls in classes}
        for s in summary:
            cls = SUMMARY_CLASSES.get((s.get("typeName") or "").lower(), category_for(s.get("typeName") or "")[0])
            price = s.get("lowestCruisePrice") or parse_price(s.get("priceRange"))
            if price:
                prices[cls] = min(float(price), prices.get(cls) or float("inf"))
        if not summary and not sailing.get("soldOut") and site.key == "ocean" and sailing.get("lowestPrice"):
            # Far-out ocean sailings only carry a "from" fare. Viking ocean ships are
            # all-veranda, so the lowest fare is a veranda (Balcony) fare.
            prices["Balcony"] = float(sailing["lowestPrice"])
        return prices
