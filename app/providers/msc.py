"""MSC Cruises (US): live fares straight from the APIs behind msccruisesusa.com.

The Booking SPA's own API (services.msccruises.com/booking/SearchCruisesB2c) sends
and receives encrypted bodies, so it isn't usable. Instead we use two plain JSON
APIs the site itself calls:

  * fares / sailing lookup — the "find a cruise" search BFF
      algoliabff-prod-eastus2-001.msccruises.com/v4/search/itineraries
    Each hit is the cheapest fare for one cruise, filtered by stateroom macro
    category (INS/OUT/BAL/SUI/YTC) and fare type (priceTypes, e.g. EZOB = "CRUISE
    ONLY OBC INCLUDED", EZPF = "CRUISE WITH DRINKS WIFI OBC"). These are the same
    "From $X pp" figures as step B of the Booking SPA (which rounds up to the next
    dollar, so it can show $1 more). Prices are per person for 2 guests and
    include port charges/taxes (`portCharges`).
  * itinerary — services.msccruises.com/itinerary/data/<cruiseId>/ (ports, times).

Both sit behind Akamai. From a datacenter IP they answer when sent a full
browser header set; if they start refusing, we run the same fetches from inside
an msccruisesusa.com page loaded through Cloudflare Browser Rendering.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Iterator, Optional
from urllib.parse import unquote, urlencode

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest

log = logging.getLogger(__name__)

SEARCH_URL = "https://algoliabff-prod-eastus2-001.msccruises.com/v4/search/itineraries"
# Same index, but one hit per cruise instead of one per itinerary (used by the catalog).
CRUISES_URL = "https://algoliabff-prod-eastus2-001.msccruises.com/v1/search/cruises/"
ITINERARY_URL = "https://services.msccruises.com/itinerary/data/{cid}/"
SITE = "https://www.msccruisesusa.com"

# Akamai scores the whole header set; the UA alone (or a partial set) gets a 401.
BROWSER_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US",
    "origin": SITE,
    "referer": SITE + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Ship name → MSC ship code, taken from the live search index's shipCd facet
# (Sept 2026). The code is checked against the ship name MSC returns.
SHIP_CODES = {
    "msc world europa": "EU",
    "msc world asia": "AS",
    "msc world america": "AM",
    "msc world atlantic": "AT",
    "msc seaview": "SV",
    "msc virtuosa": "VI",
    "msc seashore": "SH",
    "msc seaside": "SE",
    "msc seascape": "SC",
    "msc fantasia": "FA",
    "msc musica": "MU",
    "msc divina": "DI",
    "msc preziosa": "PR",
    "msc magnifica": "MA",
    "msc splendida": "SP",
    "msc grandiosa": "GR",
    "msc euribia": "ER",
    "msc orchestra": "OR",
    "msc sinfonia": "SX",
    "msc meraviglia": "MR",
    "msc lirica": "LX",
    "msc opera": "OX",
    "msc armonia": "AX",
    "msc poesia": "PO",
    "msc bellissima": "BE",
}

# MSC macro category → (our category, display name), in MSC's own order.
MACRO_CATEGORIES = [
    ("INS", "Interior", "Interior"),
    ("OUT", "Ocean View", "Ocean View"),
    ("BAL", "Balcony", "Balcony"),
    ("SUI", "Suite", "Suite"),
    ("YTC", "Suite", "MSC Yacht Club"),
]

CRUISE_ID_RE = re.compile(r"\b([A-Z]{2}\d{8}[A-Z0-9]{6})\b")


class _Blocked(Exception):
    pass


def ship_code_for(ship: str) -> Optional[str]:
    s = ship.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    key = re.sub(r"\s+", " ", s.lower())
    if not key.startswith("msc "):
        key = f"msc {key}"
    return SHIP_CODES.get(key)


def cruise_id_from(*texts: str) -> Optional[str]:
    for t in texts:
        m = CRUISE_ID_RE.search((t or "").upper())
        if m:
            return m.group(1)
    return None


def is_drinks_fare(hit: dict) -> bool:
    return "DRINK" in (hit.get("priceDesc") or "").upper()


def onboard_credit(hit: dict) -> Optional[int]:
    """`itemDesc` carries promo codes; SB080USD = $80 onboard credit."""
    m = re.search(r"SB(\d+)USD", hit.get("itemDesc") or "")
    return int(m.group(1)) if m else None


# itemDesc experience codes → MSC experience names (verified against the Booking SPA).
EXPERIENCES = {"1": "Bella", "2": "Fantastica", "3": "Aurea", "YC": "Yacht Club"}


def experience(hit: dict) -> Optional[str]:
    """`itemDesc` like "OBS_SB100USD#EXP2B#..." → "Fantastica"."""
    for token in re.split(r"[#_;]", hit.get("itemDesc") or ""):
        m = re.fullmatch(r"EXP(YC|\d)B?", token)
        if m:
            return EXPERIENCES.get(m.group(1))
    return None


def _money(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def _time(t: Optional[str]) -> Optional[str]:
    t = (t or "").strip()
    return t[:5] if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", t) else None


class MSCProvider:
    name = "MSC Cruises (live)"

    cruise_line = "MSC"

    def __init__(self, cf: CloudflareBrowser, http: Optional[httpx.Client] = None, workers: int = 6):
        self.cf = cf
        self.http = http or httpx.Client(timeout=60, headers=BROWSER_HEADERS)
        self.workers = workers
        self.browser_batch = 10  # URLs per Cloudflare page load (responses land in the returned HTML)
        self._use_browser = False
        self._sleep = time.sleep

    def handles(self, cruise_line: str) -> bool:
        return "msc" in cruise_line.lower()

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        wanted_id = cruise_id_from(req.booking_url, req.ship)
        code = wanted_id[:2] if wanted_id else ship_code_for(req.ship)
        if not code:
            raise ProviderError(f"Unknown MSC ship '{req.ship}' — enter its two-letter MSC ship code")
        adults = 1 if req.adults == 1 else 2

        hits = self._search(ship=code, departureDateFrom=req.sail_date, departureDateTo=req.sail_date,
                            noofAdults=adults, hitsPerPage=20)["hits"]
        hit, others = self.pick_sailing(hits, code, req.sail_date, wanted_id)
        if not hit:
            raise ProviderError(f"No MSC sailing found for ship {code} on {req.sail_date}")
        ship_name = (hit.get("shipCd") or {}).get("value") or req.ship
        if ship_code_for(ship_name) not in (code, None):
            raise ProviderError(f"MSC code {code} is {ship_name}, not {req.ship}")
        cid = hit["cruiseID"]

        booking_url = f"{SITE}/Booking?CruiseID={cid}"
        research = SailingResearch(
            cruise_line="MSC Cruises",
            ship=ship_name,
            sail_date=req.sail_date,
            nights=hit.get("numberOfNights"),
            departure_port=(hit.get("embkPort") or {}).get("value"),
            itinerary_name=hit.get("itineraryName"),
            itinerary=[],
            currency="USD",
            staterooms=[],
            addons=[],
            sources=[booking_url],
            warnings=[],
        )
        warnings = research.warnings
        if others:
            alts = ", ".join(f"{o['cruiseID']}, {o.get('numberOfNights')} nights" for o in others)
            warnings.append(
                f"MSC has other sailings of {ship_name} on {req.sail_date} "
                f"({alts}); "
                f"quoted {cid}. Paste its booking URL to pick another."
            )
        if req.adults > 2 or req.children:
            warnings.append(
                "MSC fares are per person based on 2 guests. 3rd/4th guests and children "
                "(Kids Sail Free promotions) are priced differently — check the MSC booking page."
            )

        # Facets for this cruise tell us which categories and fare types exist.
        facets_q = self._query_url(cruiseIdList=cid, noofAdults=adults, hitsPerPage=1, includeFacets="true")
        itin_url = ITINERARY_URL.format(cid=cid) + "?" + urlencode({"cruiseid": cid, "lang": "en-US"})
        facets_raw, itin_raw = self._get_many([facets_q, itin_url])

        if isinstance(itin_raw, dict) and itin_raw.get("Days"):
            research.itinerary = self.map_days(itin_raw["Days"], req.sail_date)
            research.itinerary_name = itin_raw.get("ItineraryName") or research.itinerary_name
            research.departure_port = itin_raw.get("DeparturePort") or research.departure_port
            research.sources.append(f"MSC itinerary data ({ITINERARY_URL.format(cid=cid)})")
        else:
            warnings.append("The MSC itinerary (ports and times) could not be loaded.")

        facets = (facets_raw.get("facets") if isinstance(facets_raw, dict) else None) or {}
        fares = self.fetch_fares(cid, adults, facets)
        research.staterooms, deltas = self.build_staterooms(fares, booking_url)
        rooms = self.build_room_types(fares, booking_url)
        if rooms:
            research.staterooms = rooms  # one row per MSC category code; class lead-ins are the fallback
        missing = [label for key, _, label in MACRO_CATEGORIES if key not in fares]
        if missing:
            warnings.append(
                f"MSC lists no fares for: {', '.join(missing)} (sold out or not offered on this ship)."
            )
        if research.staterooms:
            warnings.append(
                "MSC prices are per person for 2 guests and include taxes & port fees (broken out as taxes/fees); "
                "MSC's booking page rounds up, so it may show $1 more."
            )
        drinks = self.drinks_addon(deltas, research.nights, booking_url)
        if drinks:
            research.addons.append(drinks)
        return research

    # ── Fares ────────────────────────────────────────────────────────────

    def fetch_fares(self, cid: str, adults: int, facets: dict) -> dict[str, list[dict]]:
        """Every fare hit for the cruise, grouped by macro category.

        The search collapses results to one hit per cruise, so we ask for one
        category code at a time: an exact-phrase query ("BR1") matches the hit's
        category key (unquoted, 2-letter codes prefix-match words like "Bahamas").
        One cruise-only and one Drinks & Wi-Fi query per code, run in parallel.
        """
        codes = sorted(facets.get("category.key") or {})
        price_types = sorted(facets.get("priceType") or {})
        common = {"cruiseIdList": cid, "noofAdults": adults, "hitsPerPage": 5}
        fares: dict[str, list[dict]] = {}

        def keep(raw: Any, check) -> None:
            for h in (raw or {}).get("hits", []) if isinstance(raw, dict) else []:
                if h.get("cruiseID") == cid and check(h):
                    fares.setdefault((h.get("macroCategory") or {}).get("key"), []).append(h)

        if codes and price_types:
            # Which fare types are the drinks bundle? One tiny query per type tells us.
            samples = self._get_many([self._query_url(priceTypes=t, **{**common, "hitsPerPage": 1})
                                      for t in price_types])
            drinks_types = {t for t, raw in zip(price_types, samples)
                            if isinstance(raw, dict) and raw.get("hits") and is_drinks_fare(raw["hits"][0])}
            groups = [g for g in (sorted(set(price_types) - drinks_types), sorted(drinks_types)) if g]
            queries = [(c, g) for c in codes for g in groups]
            results = self._get_many([
                self._query_url(query=f'"{c}"', priceTypes=",".join(g), **common) for c, g in queries
            ])
            for (c, _), raw in zip(queries, results):
                keep(raw, lambda h, c=c: (h.get("category") or {}).get("key") == c)
            if fares:
                return fares

        # Fallback: class lead-ins only (one query per class and fare type).
        macros = [m for m in MACRO_CATEGORIES if m[0] in (facets.get("macroCategory.key") or {})] or MACRO_CATEGORIES
        queries = [(m[0], t) for m in macros for t in (price_types or [""])]
        results = self._get_many([self._query_url(macroCategory=m, priceTypes=t, **common) for m, t in queries])
        for (m, _), raw in zip(queries, results):
            keep(raw, lambda h, m=m: (h.get("macroCategory") or {}).get("key") == m)
        return fares

    # ── Transport ────────────────────────────────────────────────────────

    def _query_url(self, _url: str = SEARCH_URL, **params: Any) -> str:
        base = {"country": "US", "lang": "en", "includeResults": "true", "includeFacets": "false",
                "page": 0, "sortBy": "price", "sortOrder": "asc"}
        base.update({k: v for k, v in params.items() if v not in (None, "")})
        return _url + "?" + urlencode(base)

    def _search(self, **params: Any) -> dict:
        raw = self._get_many([self._query_url(**params)])[0]
        if not isinstance(raw, dict) or "hits" not in raw:
            raise ProviderError("MSC cruise search returned no data")
        return raw

    def _get_many(self, urls: list[str]) -> list[Any]:
        """JSON for each URL (None where one failed). Direct first, a few in parallel;
        through the Cloudflare browser once direct is blocked."""
        if not urls:
            return []
        if not self._use_browser:
            try:
                if len(urls) == 1:
                    return [self._get_direct(urls[0])]
                with ThreadPoolExecutor(max_workers=min(self.workers, len(urls))) as pool:
                    return list(pool.map(self._get_direct, urls))
            except _Blocked as exc:
                log.warning("MSC direct API blocked (%s); switching to Cloudflare browser", exc)
                if not self.cf.configured:
                    raise ProviderError(
                        f"MSC blocked the request ({exc}). Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN."
                    ) from exc
                self._use_browser = True
        chunks = [urls[i:i + self.browser_batch] for i in range(0, len(urls), self.browser_batch)]
        if len(chunks) == 1:
            return self._get_via_browser(chunks[0])
        with ThreadPoolExecutor(max_workers=min(self.workers, len(chunks))) as pool:
            return [r for part in pool.map(self._get_via_browser, chunks) for r in part]

    def _get_direct(self, url: str) -> Any:
        for attempt in range(4):
            try:
                resp = self.http.get(url, headers=BROWSER_HEADERS)
            except httpx.HTTPError as exc:
                if attempt < 3:
                    self._sleep(2 ** attempt)
                    continue
                raise _Blocked(str(exc)) from exc
            if resp.status_code in (429, 502, 503, 504) and attempt < 3:
                retry = resp.headers.get("retry-after", "")
                self._sleep(float(retry) if retry.isdigit() else 2 ** attempt)
                continue
            if resp.status_code in (401, 403, 429):
                raise _Blocked(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except ValueError:
                raise _Blocked("non-JSON response")
        return None

    def _get_via_browser(self, urls: list[str]) -> list[Any]:
        # Load an msccruisesusa.com page (Akamai cookies, real browser) and run the
        # fetches from inside it, stashing the results in a DOM node we read back.
        script = (
            "(async function(){var urls=" + json.dumps(urls) + ";var out=await Promise.all(urls.map(function(u){"
            "return fetch(u,{headers:{accept:'application/json'}}).then(function(r){return r.ok?r.text():null;})"
            ".catch(function(){return null;});}));"
            "var d=document.createElement('div');d.id='qp-msc';"
            "d.setAttribute('data-json',encodeURIComponent(JSON.stringify(out)));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content(
                {
                    "url": SITE + "/",
                    "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                    "addScriptTag": [{"content": script}],
                    "waitForSelector": {"selector": "#qp-msc", "timeout": 55000},
                }
            )
        except BrowserRenderingError as exc:
            raise ProviderError(f"MSC lookup through the browser failed: {exc}") from exc
        node = BeautifulSoup(html, "html.parser").find(id="qp-msc")
        if node is None:
            raise ProviderError("MSC lookup through the browser returned no data")
        out = []
        for text in json.loads(unquote(node.get("data-json", "")) or "[]"):
            try:
                out.append(json.loads(text) if text else None)
            except ValueError:
                out.append(None)
        return out + [None] * (len(urls) - len(out))

    # ── Parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def pick_sailing(hits: list[dict], ship_code: str, sail_date: str,
                     wanted_id: Optional[str] = None) -> tuple[Optional[dict], list[dict]]:
        """The sailing on `sail_date`; prefers an explicit cruise ID, else the shortest
        (MSC also lists back-to-back combinations, e.g. AM20270306MIAMI1 = 14 nights)."""
        same = [
            h for h in hits
            if (h.get("cruiseID") or "").startswith(ship_code)
            and (h.get("departureStartDate") or "")[:10] == sail_date
        ]
        if not same:
            return None, []
        exact = [h for h in same if h.get("cruiseID") == wanted_id]
        pick = exact[0] if exact else min(same, key=lambda h: (h.get("numberOfNights") or 999, h["cruiseID"]))
        return pick, [h for h in same if h is not pick]

    @staticmethod
    def map_days(days: list[dict], sail_date: str) -> list[ItineraryDay]:
        start = date.fromisoformat(sail_date)
        out = []
        for i, d in enumerate(days):
            sea = bool(d.get("IsSeaDay")) or d.get("PortCode") == "SEADAY"
            out.append(
                ItineraryDay(
                    day=i + 1,
                    date=(d.get("Date") or "")[:10] or (start + timedelta(days=i)).isoformat(),
                    port="At Sea" if sea else (d.get("Name") or "Port"),
                    # MSC fills sea days with placeholder times; drop them.
                    arrive=None if sea else _time(d.get("ArrivalTime")),
                    depart=None if sea else _time(d.get("DepartureTime")),
                )
            )
        return out

    @staticmethod
    def build_staterooms(fares: dict[str, list[dict]], source: str) -> tuple[list[Stateroom], dict[str, float]]:
        """One row per MSC category at the cruise-only fare, with the Drinks & Wi-Fi
        fare in the notes. Returns the rooms and the per-category drinks delta."""
        rooms, deltas = [], {}
        for key, category, label in MACRO_CATEGORIES:
            hits = [h for h in fares.get(key, []) if (h.get("prices") or {}).get("availability", True)]
            if not hits:
                continue
            price = lambda h: (h.get("prices") or {}).get("adultPrice") or 0  # noqa: E731
            cruise_only = [h for h in hits if not is_drinks_fare(h)]
            drinks = [h for h in hits if is_drinks_fare(h)]
            # Cheapest cruise-only fare; on a tie prefer the one with onboard credit (what MSC shows first).
            base = min(cruise_only or hits, key=lambda h: (price(h), -(onboard_credit(h) or 0)))
            total = float(price(base))
            taxes = float((base.get("prices") or {}).get("portCharges") or 0)
            fare_name = "Cruise-only" if not is_drinks_fare(base) else "Drinks & Wi-Fi"
            notes = [f"{fare_name} fare ({base.get('priceType') or 'MSC'}): MSC shows {_money(total)} pp incl. "
                     f"{_money(taxes)} taxes & fees."]
            if drinks:
                with_drinks = float(price(min(drinks, key=price)))
                delta = with_drinks - total
                if key == "YTC" or delta <= 0:
                    notes.append(f"With Drinks & Wi-Fi: {_money(with_drinks)} pp — "
                                 "drinks & Wi-Fi already included in the Yacht Club fare.")
                else:
                    deltas[key] = delta
                    notes.append(f"With Drinks & Wi-Fi: {_money(with_drinks)} pp incl. taxes (+{_money(delta)} pp).")
            obc = onboard_credit(base)
            if obc:
                notes.append(f"Includes ${obc} onboard credit.")
            rooms.append(
                Stateroom(
                    category=category,
                    name=label,
                    code=(base.get("category") or {}).get("key"),
                    price_per_person=round(total - taxes, 2),
                    taxes_fees_per_person=taxes,
                    sold_out=False,
                    notes=" ".join(notes),
                    source=source,
                )
            )
        return rooms, deltas

    @staticmethod
    def drinks_addon(deltas: dict[str, float], nights: Optional[int], source: str) -> Optional[AddOn]:
        if not deltas:
            return None
        lo, hi = min(deltas.values()), max(deltas.values())
        labels = {k: lbl for k, _, lbl in MACRO_CATEGORIES}
        per_cat = "; ".join(f"{labels[k]} +{_money(v)}" for k, v in deltas.items())
        desc = (
            "MSC's 'Add Drinks & Wi-Fi' fare: a drinks package plus a Wi-Fi package bundled with the cruise, "
            "per person, for the whole cruise. The bundle price is only available when booking (packages bought "
            "later cost more); MSC Yacht Club already includes drinks & Wi-Fi."
        )
        if nights and lo == hi:
            desc += f" About {_money(round(lo / nights, 2))} pp per night."
        if lo != hi:
            desc += f" Varies by category: {per_cat}."
        return AddOn(
            kind="beverage",
            name="Drinks & Wi-Fi bundle (Add Drinks & Wi-Fi fare)",
            price=lo,
            price_unit="per_person",
            unit_label="per person, whole cruise" + ("" if lo == hi else " (from)"),
            port=None,
            description=desc,
            source=source,
        )

    @staticmethod
    def build_room_types(fares: dict[str, list[dict]], source: str) -> list[Stateroom]:
        """One Stateroom per MSC category code (e.g. BR1), cruise-only fare, with the
        Drinks & Wi-Fi fare for the same code in the notes."""
        by_code: dict[str, list[dict]] = {}
        for hits in fares.values():
            for h in hits:
                code = (h.get("category") or {}).get("key")
                if code and (h.get("prices") or {}).get("availability", True):
                    by_code.setdefault(code, []).append(h)
        order = {key: i for i, (key, _, _) in enumerate(MACRO_CATEGORIES)}
        price = lambda h: (h.get("prices") or {}).get("adultPrice") or 0  # noqa: E731
        rows = []
        for code, hits in by_code.items():
            cruise_only = [h for h in hits if not is_drinks_fare(h)]
            if not cruise_only:
                continue
            base = min(cruise_only, key=lambda h: (price(h), -(onboard_credit(h) or 0)))
            macro = (base.get("macroCategory") or {}).get("key")
            category, label = next(((c, lbl) for k, c, lbl in MACRO_CATEGORIES if k == macro), ("Other", macro or "Stateroom"))
            exp = experience(base)
            name = label if exp in (None, "Yacht Club") and macro == "YTC" else f"{label} {exp}" if exp else label
            total = float(price(base))
            taxes = float((base.get("prices") or {}).get("portCharges") or 0)
            notes = [f"Cruise-only fare ({base.get('priceType') or 'MSC'}): MSC shows {_money(total)} pp incl. "
                     f"{_money(taxes)} taxes & fees."]
            drinks = [h for h in hits if is_drinks_fare(h)]
            if drinks:
                with_drinks = float(price(min(drinks, key=price)))
                if macro == "YTC" or with_drinks <= total:
                    notes.append(f"With Drinks & Wi-Fi: {_money(with_drinks)} pp — already included in this fare.")
                else:
                    notes.append(f"With Drinks & Wi-Fi: {_money(with_drinks)} pp incl. taxes "
                                 f"(+{_money(with_drinks - total)} pp).")
            obc = onboard_credit(base)
            if obc:
                notes.append(f"Includes ${obc} onboard credit.")
            rows.append((order.get(macro, 9), total, Stateroom(
                category=category,
                name=f"{name} ({code})",
                code=code,
                price_per_person=round(total - taxes, 2),
                taxes_fees_per_person=taxes,
                sold_out=False,
                notes=" ".join(notes),
                source=source,
            )))
        return [r for _, _, r in sorted(rows, key=lambda r: (r[0], r[1], r[2].code))]

    # ── Bulk catalog ─────────────────────────────────────────────────────

    def iter_catalog(self) -> Iterator[CatalogSailing]:
        """Every bookable MSC sailing sold on the US site, with lead-in fares per class.

        One facet call lists the ships; then per ship one /v1/search/cruises call per
        room class (hitsPerPage=1000; a ship has ~100-250 sailings), so ~5 calls per ship.
        (The /v4 itineraries search collapses to one sailing per itinerary, so it can't be used.)
        Cheapest fare per class is the cruise-only fare (the drinks bundle only ever
        costs more, or the same for Yacht Club).
        """
        raw = self._search(noofAdults=2, hitsPerPage=1, includeFacets="true")
        ships = sorted(((raw.get("facets") or {}).get("shipCd.key") or {}))
        if not ships:
            raise ProviderError("MSC search returned no ships")
        jobs = [(ship, key) for ship in ships for key, _, _ in MACRO_CATEGORIES]
        url = lambda ship, key, page=0: self._query_url(  # noqa: E731
            CRUISES_URL, ship=ship, macroCategory=key, noofAdults=2, hitsPerPage=1000, page=page)
        results = self._get_many([url(ship, key) for ship, key in jobs])
        calls = 1 + len(jobs)
        pages: dict[tuple[str, str], list[dict]] = {}
        for job, res in zip(jobs, results):
            hits = list((res or {}).get("hits", [])) if isinstance(res, dict) else []
            if res is None:
                log.warning("MSC catalog: no data for ship %s / %s", *job)
            n_pages = (res or {}).get("nbPages") or 1 if isinstance(res, dict) else 1
            for page in range(1, n_pages):  # a ship has ~100-250 sailings, so rarely needed
                more = self._get_many([url(*job, page=page)])[0]
                calls += 1
                hits += (more or {}).get("hits", []) if isinstance(more, dict) else []
            pages[job] = hits
        self.catalog_calls = calls

        for ship in ships:
            sailings: dict[str, dict] = {}
            prices: dict[str, dict[str, float]] = {}
            offered: set[str] = set()
            for key, _, label in MACRO_CATEGORIES:
                for h in pages.get((ship, key), []):
                    cid = h.get("cruiseID")
                    if not cid or (h.get("macroCategory") or {}).get("key") != key:
                        continue
                    sailings.setdefault(cid, h)
                    offered.add(label)
                    p = h.get("prices") or {}
                    if p.get("availability", True) and p.get("adultPrice") is not None:
                        prices.setdefault(cid, {})[label] = round(float(p["adultPrice"]) - float(p.get("portCharges") or 0), 2)
            for cid, h in sorted(sailings.items(), key=lambda kv: (kv[1].get("departureStartDate") or "", kv[0])):
                got = prices.get(cid, {})
                taxes = (h.get("prices") or {}).get("portCharges")
                yield CatalogSailing(
                    cruise_line=self.cruise_line,
                    sailing_key=cid,
                    ship=(h.get("shipCd") or {}).get("value") or ship,
                    sail_date=(h.get("departureStartDate") or "")[:10],
                    nights=h.get("numberOfNights"),
                    ship_code=ship,
                    itinerary_name=h.get("itineraryName"),
                    departure_port=(h.get("embkPort") or {}).get("value"),
                    ports=[p.get("value") for p in h.get("visitingPorts") or []
                           if p.get("key") != "SEADAY" and p.get("value")],
                    booking_url=f"{SITE}/Booking?CruiseID={cid}",
                    # Classes this ship sells but this sailing doesn't list → None (sold out).
                    prices={lbl: got.get(lbl) for _, _, lbl in MACRO_CATEGORIES if lbl in offered},
                    taxes_fees_per_person=float(taxes) if taxes is not None else None,
                    currency="USD",
                )
