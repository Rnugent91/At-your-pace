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
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import unquote, urlencode

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import ProviderError, ResearchRequest

log = logging.getLogger(__name__)

SEARCH_URL = "https://algoliabff-prod-eastus2-001.msccruises.com/v4/search/itineraries"
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


def _money(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def _time(t: Optional[str]) -> Optional[str]:
    t = (t or "").strip()
    return t[:5] if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", t) else None


class MSCProvider:
    name = "MSC Cruises (live)"

    def __init__(self, cf: CloudflareBrowser, http: Optional[httpx.Client] = None):
        self.cf = cf
        self.http = http or httpx.Client(timeout=30, headers=BROWSER_HEADERS)
        self._use_browser = False

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
        macros = [m for m in MACRO_CATEGORIES if m[0] in (facets.get("macroCategory.key") or {})]
        price_types = sorted(facets.get("priceType") or {}) or [""]
        if not facets.get("macroCategory.key"):
            macros = MACRO_CATEGORIES

        queries = [(m, t) for m in macros for t in price_types]
        results = self._get_many([
            self._query_url(cruiseIdList=cid, noofAdults=adults, hitsPerPage=5, macroCategory=m[0], priceTypes=t)
            for m, t in queries
        ])
        fares: dict[str, list[dict]] = {}
        for (m, _), raw in zip(queries, results):
            for h in (raw or {}).get("hits", []) if isinstance(raw, dict) else []:
                if h.get("cruiseID") == cid and (h.get("macroCategory") or {}).get("key") == m[0]:
                    fares.setdefault(m[0], []).append(h)

        research.staterooms, deltas = self.build_staterooms(fares, booking_url)
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

    # ── Transport ────────────────────────────────────────────────────────

    def _query_url(self, **params: Any) -> str:
        base = {"country": "US", "lang": "en", "includeResults": "true", "includeFacets": "false",
                "page": 0, "sortBy": "price", "sortOrder": "asc"}
        base.update({k: v for k, v in params.items() if v not in (None, "")})
        return SEARCH_URL + "?" + urlencode(base)

    def _search(self, **params: Any) -> dict:
        raw = self._get_many([self._query_url(**params)])[0]
        if not isinstance(raw, dict) or "hits" not in raw:
            raise ProviderError("MSC cruise search returned no data")
        return raw

    def _get_many(self, urls: list[str]) -> list[Any]:
        """JSON for each URL (None where one failed). Direct first; browser once direct is blocked."""
        if not self._use_browser:
            try:
                return [self._get_direct(u) for u in urls]
            except _Blocked as exc:
                log.warning("MSC direct API blocked (%s); switching to Cloudflare browser", exc)
                if not self.cf.configured:
                    raise ProviderError(
                        f"MSC blocked the request ({exc}). Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN."
                    ) from exc
                self._use_browser = True
        return self._get_via_browser(urls)

    def _get_direct(self, url: str) -> Any:
        try:
            resp = self.http.get(url, headers=BROWSER_HEADERS)
        except httpx.HTTPError as exc:
            raise _Blocked(str(exc)) from exc
        if resp.status_code in (401, 403, 429):
            raise _Blocked(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            raise _Blocked("non-JSON response")

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
