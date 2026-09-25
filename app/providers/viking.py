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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import USER_AGENT, BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import ProviderError, ResearchRequest
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

    def handles(self, cruise_line: str) -> bool:
        return "viking" in cruise_line.lower()

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

        if page_url:
            try:
                research.itinerary, itin_url = self.fetch_itinerary(
                    site, page_url, entry.get("cruiseDirection") or "", req.sail_date
                )
                if research.itinerary and itin_url not in research.sources:
                    research.sources.append(itin_url)
            except ProviderError as exc:
                log.warning("Viking itinerary fetch failed: %s", exc)
                warnings.append(f"Day-by-day itinerary could not be loaded from Viking: {exc}")

        research.addons = self.build_addons(site, entry, warnings)
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
                try:
                    resp = self.http.post(
                        url,
                        content=json.dumps({k: v for k, v in body.items() if not k.startswith("__")}),
                        headers={
                            "content-type": "application/json; charset=utf-8",
                            "x-requested-with": "XMLHttpRequest",
                            "origin": site.origin,
                            "referer": site.base + "/",
                        },
                    )
                except httpx.HTTPError as exc:
                    blocked.append(str(exc))
                    return None
                if resp.status_code in (401, 403, 429):
                    blocked.append(f"HTTP {resp.status_code}")
                    return None
                try:
                    return resp.json() if resp.status_code == 200 else None
                except ValueError:
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
            "return c.DepartureDateString===bodies[i].__date;});r.calendar=null;}}catch(e){r=null;}out.push(r);}"
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

    def fetch_itinerary(self, site: Site, page_url: str, direction: str, sail_date: str) -> tuple[list[ItineraryDay], str]:
        """Day-by-day from the cruise page for this sailing's direction and year."""
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
        return days, url

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

    def build_addons(self, site: Site, entry: dict, warnings: list[str]) -> list[AddOn]:
        addons: list[AddOn] = []
        price = self.silver_spirits_price(site)
        ss_url = f"{site.base}/my-trip/silver-spirits-beverage-package/index.html"
        if price is not None:
            addons.append(
                AddOn(
                    kind="beverage",
                    name="Silver Spirits Beverage Package",
                    price=price,
                    price_unit="per_person_per_day",
                    unit_label="per guest, per day",
                    port=None,
                    description="Wines, spirits & cocktails all day. Beer, wine & soft drinks with lunch "
                    "and dinner, Wi-Fi and one shore excursion per port are already included in the fare.",
                    source=ss_url,
                )
            )
        else:
            warnings.append("Couldn't read the Silver Spirits Beverage Package price from Viking.")
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
                        f"Lowest Viking Air add-on for this sailing (advertised {entry['advertisedAirFare']})."
                        if entry.get("advertisedAirFare")
                        else "Lowest Viking Air add-on for this sailing."
                    ),
                    source="Viking Dates & Pricing",
                )
            )
        warnings.append(
            "Optional shore excursion prices aren't published by Viking before booking; "
            "one excursion per port is included in the fare."
        )
        return addons

    def silver_spirits_price(self, site: Site) -> Optional[float]:
        try:
            html = self.get_html(f"{site.base}/my-trip/silver-spirits-beverage-package/index.html")
        except ProviderError:
            return None
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        m = re.search(r"Silver Spirits Beverage Package\s*\(\s*\$(\d+(?:\.\d+)?)\s*USD\s*/\s*Day", text, re.I) or re.search(
            r"\$(\d+(?:\.\d+)?)\s*USD\s*(?:per|/)\s*day", text, re.I
        )
        return float(m.group(1)) if m else None
