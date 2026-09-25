"""Disney Cruise Line: live prices from disneycruise.disney.go.com's own JSON services.

Everything comes from the two backends the Find-a-Cruise and stateroom-picker SPAs call:

  productavail-vas  (search)
    POST authz/private                    → sets the guest session (no body needed)
    POST available-products/              → product/itinerary cards, 5 per page, with
                                            per-class lead-in prices (no sail dates!)
    POST available-sailings/              → every sailing of one product+itinerary with its
                                            dates and per-class lead-in prices
  sailingavailability-vas  (one sailing; needs `BEARER <client-token>`)
    GET  client-token/                    → {"access_token", "expires_in": 1800}
    POST get-cruise-details-availability/{sailingId}?region=INTL
                                          → day-by-day itinerary with times, ports, ship,
                                            gratuities, and the lowest fare of every
                                            stateroom sub-type incl. guarantees (IGT/OGT/VGT)
    POST stateroom-category-search        → fare of every stateroom category (11C, 9D, 5A,
                                            4E, 03A...) for the party, with per-guest
                                            subtotal and taxes

Fares depend on the party mix, so research() sends req.adults adults and req.children
children (priced as age 8; Disney charges by age, so the advisor should re-check with
real ages). "subtotal" is the cruise fare; "tax" is taxes, fees and port expenses.

Disney sits behind Akamai Bot Manager and Queue-it. Plain requests with browser headers
work from a datacenter IP today (the vas APIs aren't queue-protected); if they're
refused, the same calls run inside a real browser via Cloudflare Browser Rendering (a
tiny same-origin page is loaded and the calls are made with fetch() in-page).
"""

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Iterator, Optional
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from ..cf_browser import BrowserRenderingError, CloudflareBrowser
from ..models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .base import CatalogSailing, ProviderError, ResearchRequest

log = logging.getLogger(__name__)

BASE = "https://disneycruise.disney.go.com"
PA = "/dcl-apps-productavail-vas/"
SA = "/dcl-apps-sailingavailability-vas/"
# Small same-origin HTML page used to run the API calls in a real browser.
BROWSER_PAGE = BASE + "/authenticator/responder.html?clientId=TPR-DCL-LBJS.WEB&environment=PROD"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "user-agent": USER_AGENT,
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": BASE,
    "referer": BASE + "/cruises-destinations/list/",
    "x-use-voyage-svc": "true",
    "x-dash-phase-one": "true",
    "x-bypass-product-avail-svc": "false",
    "x-disney-internal-is-cast": "false",
}

# Ship name → Disney "seaware" ship code, as listed by quick-quote-filter-options and
# returned on every sailing (verified 2026-09). Disney Believe (2027) isn't bookable yet.
SHIP_CODES = {
    "disney magic": "DM",
    "disney wonder": "DW",
    "disney dream": "DD",
    "disney fantasy": "DF",
    "disney wish": "WW",
    "disney treasure": "WT",
    "disney destiny": "WD",
    "disney adventure": "DA",
}
SHIP_NAMES = {code: name.title() for name, code in SHIP_CODES.items()}

# Stateroom type (the suffix of "DD-VERANDAH") → our category.
TYPE_CATEGORY = {"INSIDE": "Interior", "OUTSIDE": "Ocean View", "VERANDAH": "Balcony", "SUITE": "Suite"}
TYPE_ORDER = {"INSIDE": 0, "OUTSIDE": 1, "VERANDAH": 2, "SUITE": 3}
CHILD_AGE = 8  # age sent for each child when real ages aren't known

WORKERS = 4
BROWSER_BATCH = 8  # API calls per rendered page (Cloudflare pages time out at 60s)

# Keys dropped from responses in the browser path (marketing copy/images) to keep the
# rendered HTML small. The search results' per-sailing ship.stateroomTypes blocks are
# dropped too (ship objects that carry a seawareId).
STRIP_KEYS = ["media", "descriptions", "productItineraryData", "webLinks", "themes",
              "itineraryAtGlance", "destinations"]


def ship_code_for(ship: str) -> Optional[str]:
    s = re.sub(r"\s+", " ", (ship or "").strip().lower())
    if not s:
        return None
    if s.upper() in SHIP_NAMES:
        return s.upper()
    if s in SHIP_CODES:
        return SHIP_CODES[s]
    if not s.startswith("disney "):
        s2 = "disney " + s
        if s2 in SHIP_CODES:
            return SHIP_CODES[s2]
    for name, code in SHIP_CODES.items():
        if s in name or name.split()[-1] in s.split():
            return code
    return None


def sailing_id_from_url(url: str) -> Optional[str]:
    """DD1529 from .../cruises-destinations/list/DD1529/<product>/<date-ship>/ or /select-stateroom/DD1529/..."""
    m = re.search(r"/(?:list|select-stateroom|cruise-details)/([A-Z]{2}\d{3,5})(?:[/?#]|$)", url or "")
    return m.group(1) if m else None


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")


def booking_url(sailing_id: str, product_name: str, sail_date: str, ship_name: str) -> str:
    return f"{BASE}/cruises-destinations/list/{sailing_id}/{slug(product_name)}/{sail_date}-{slug(ship_name)}/"


def party_mix(adults: int, children: int, sailing_avail: bool = False) -> list[dict]:
    kids = [{"age": CHILD_AGE, "ageUnit": "YEAR"} for _ in range(max(children, 0))]
    pm = {"accessible": False, "adultCount": max(adults, 1), "childCount": len(kids),
          "nonAdultAges": kids, "partyMixId": "0"}
    if sailing_avail:
        pm = {"number": 1, "isDefault": True, **pm, "stateroomInfo": {}}
    return [pm]


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _entity(ref: Optional[str]) -> str:
    """'DD-11B;entityType=stateroom-category;destination=dcl' → 'DD-11B'."""
    return (ref or "").split(";")[0]


def _type_key(stateroom_type: str) -> str:
    """'DD-VERANDAH' → 'VERANDAH'."""
    return _entity(stateroom_type).split("-", 1)[-1]


def _guests(price: Optional[dict]) -> list[dict]:
    """breakdownByGuest comes as a {"1": {...}} map or a [{"guestId": "1"}] list."""
    g = (price or {}).get("breakdownByGuest") or []
    if isinstance(g, dict):
        return [g[k] for k in sorted(g, key=lambda k: int(k) if str(k).isdigit() else 99)]
    return list(g)


def fare_per_person(price: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    """(cruise fare, taxes) for the first adult; (None, None) when there's no price."""
    guests = _guests(price)
    adult = next((g for g in guests if (g.get("ageGroup") or "ADULT") == "ADULT"), guests[0] if guests else None)
    if not adult:
        return None, None
    return _num(adult.get("subtotal")), _num(adult.get("tax"))


def party_total(price: Optional[dict]) -> Optional[float]:
    return _num(((price or {}).get("summary") or {}).get("total"))


def departure_from_name(product_name: str) -> Optional[str]:
    m = re.search(r"\bfrom (.+)$", product_name or "")
    return m.group(1).strip() if m else None


def _hhmm(dt: Optional[str]) -> Optional[str]:
    return dt[11:16] if dt and len(dt) >= 16 else None


class DisneyProvider:
    name = "Disney Cruise Line (live)"
    cruise_line = "Disney"

    def __init__(self, cf: CloudflareBrowser, http: Optional[httpx.Client] = None):
        self.cf = cf
        self.http = http or httpx.Client(timeout=60, follow_redirects=True, headers=BROWSER_HEADERS)
        self._use_browser = False
        self._token: Optional[str] = None
        self._token_at = 0.0
        self._authz_done = False
        self._lock = threading.Lock()
        self._sleep = time.sleep
        self.calls = 0  # HTTP/API calls made (for catalog stats)

    def handles(self, cruise_line: str) -> bool:
        return "disney" in (cruise_line or "").lower()

    # ── Entry point ──────────────────────────────────────────────────────

    def research(self, req: ResearchRequest) -> SailingResearch:
        sailing_id = sailing_id_from_url(req.booking_url)
        lead: Optional[dict] = None
        product_name = ""
        if not sailing_id:
            code = ship_code_for(req.ship)
            if not code:
                raise ProviderError(f"Unknown Disney ship '{req.ship}' (known: {', '.join(SHIP_NAMES.values())})")
            try:
                date.fromisoformat(req.sail_date)
            except ValueError as exc:
                raise ProviderError(f"Bad sail date '{req.sail_date}'") from exc
            found = self.find_sailing(code, req.sail_date, req.adults, req.children)
            if not found:
                raise ProviderError(f"No Disney sailing found for {SHIP_NAMES[code]} on {req.sail_date}")
            lead, product_name = found
            sailing_id = lead["sailingId"]

        details, cats = self._post_many([
            self._details_call(sailing_id, req.adults, req.children),
            self._category_call(sailing_id, req.adults, req.children),
        ])
        if not details and not lead:
            raise ProviderError(f"Disney returned no details for sailing {sailing_id}")

        info = self.parse_details(details) if details else {}
        ship_name = info.get("ship") or (lead or {}).get("ship", {}).get("name") or req.ship
        sail_date = info.get("sail_date") or (lead or {}).get("sailDateFrom") or req.sail_date
        product_name = info.get("product") or product_name
        url = booking_url(sailing_id, product_name, sail_date, ship_name)

        research = SailingResearch(
            cruise_line="Disney",
            ship=ship_name,
            sail_date=sail_date,
            nights=info.get("nights") or (lead or {}).get("numberOfNights"),
            departure_port=info.get("departure_port") or departure_from_name(product_name),
            itinerary_name=product_name or None,
            itinerary=info.get("itinerary") or [],
            currency="USD",
            staterooms=[],
            addons=[],
            sources=[url],
            warnings=[],
        )
        warnings = research.warnings  # (pydantic copies lists passed in)
        if req.sail_date and sail_date != req.sail_date and not req.booking_url:
            warnings.append(f"Disney lists this sailing as departing {sail_date}.")

        subtypes = self.parse_subtypes(details) if details else []
        rooms = self.map_categories(cats, url, req) if cats else []
        if rooms:
            # Guarantee ("GTY") fares aren't in the category search; add them from the details,
            # replacing a sold-out category row the guarantee covers (e.g. 11C sold out, 11C GTY open).
            priced = {r.code for r in rooms if not r.sold_out}
            gty = [r for r in subtypes if r.notes and r.notes.startswith("Guarantee")
                   and not r.sold_out and r.code not in priced]
            covered = {r.code for r in gty}
            rooms = [r for r in rooms if not (r.sold_out and r.code in covered)] + gty
        elif subtypes:
            rooms = subtypes
            warnings.append("Disney's category prices were unavailable; showing the lowest fare per room type.")
        elif lead:
            rooms = self.class_leadins(lead, url)
            warnings.append("Only Disney's lowest fare per stateroom class could be loaded.")
        else:
            warnings.append("Disney returned no stateroom prices for this sailing.")
        research.staterooms = sorted(
            rooms, key=lambda r: (list(TYPE_CATEGORY.values()).index(r.category) if r.category in TYPE_CATEGORY.values() else 9,
                                  r.price_per_person is None, r.price_per_person or 0))
        if rooms and all(r.sold_out for r in rooms):
            warnings.append("Disney shows every stateroom on this sailing as sold out.")

        research.addons = self.gratuity_addons(info, url)
        if req.children:
            warnings.append(
                f"Child fares assume age {CHILD_AGE}; Disney prices children by age (infants and 3–12s differ) — re-check with real ages.")
        if not research.itinerary:
            warnings.append("The day-by-day itinerary could not be loaded from Disney.")
        return research

    # ── Sailing lookup ───────────────────────────────────────────────────

    def find_sailing(self, ship_code: str, sail_date: str, adults: int = 2, children: int = 0
                     ) -> Optional[tuple[dict, str]]:
        """→ (available-sailings entry, product name) for the ship on that date."""
        filters = [f"{ship_code};filterType=ship", f"{sail_date[:7]};filterType=date"]
        pairs = {}
        for page in self.product_pages(filters, adults, children, max_pages=10):
            for prod in page.get("products") or []:
                for itin in prod.get("itineraries") or []:
                    pairs[(prod.get("productId"), itin.get("itineraryId"))] = prod.get("productName") or ""
        if not pairs:
            return None
        keys = list(pairs)
        results = self._post_many([self._sailings_call(p, i, adults, children, filters) for p, i in keys])
        for (pid, iid), res in zip(keys, results):
            for s in (res or {}).get("sailings") or []:
                if s.get("sailDateFrom") == sail_date and (s.get("ship") or {}).get("seawareId") == ship_code:
                    return s, pairs[(pid, iid)]
        return None

    def product_pages(self, filters: list[str], adults: int = 2, children: int = 0,
                      max_pages: int = 100, workers: int = 1) -> list[dict]:
        first = self._post_many([self._products_call(filters, 1, adults, children)])[0]
        if not first:
            raise ProviderError("Disney's cruise search returned nothing")
        total = min(int(first.get("totalPages") or 1), max_pages)
        rest = [self._products_call(filters, n, adults, children) for n in range(2, total + 1)]
        return [first] + [p for p in self._post_many(rest, workers=workers) if p]

    # ── Parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def parse_details(data: dict) -> dict:
        res = (data.get("cruiseDetailsResponse") or {}).get("cruiseSailingsListResource") or {}
        sailing = next(iter((res.get("sailings") or {}).values()), {}) or {}
        ports = {k: v.get("name") for k, v in (res.get("ports") or {}).items()}
        ship = (res.get("ships") or {}).get(sailing.get("ship")) or next(iter((res.get("ships") or {}).values()), {}) or {}
        product = (res.get("products") or {}).get(sailing.get("product")) or next(iter((res.get("products") or {}).values()), {}) or {}

        days: list[ItineraryDay] = []
        itin = sailing.get("itinerary") or {}
        for n in sorted(itin, key=lambda k: int(k) if str(k).isdigit() else 999):
            events = itin[n].get("itineraryDetails") or []
            if not events:
                continue
            port_id = events[0].get("portCode")
            name = ports.get(port_id) or _entity(port_id)
            by = {e.get("sailEventType"): e.get("itineraryDateTime") for e in events}
            sea = "AT SEA" in by or name == "Day at Sea"
            days.append(ItineraryDay(
                day=int(n) if str(n).isdigit() else len(days) + 1,
                date=(events[0].get("itineraryDateTime") or "")[:10] or None,
                port="At Sea" if sea else name,
                arrive=None if sea else _hhmm(by.get("ARRIVAL")),
                depart=None if sea else _hhmm(by.get("DEPARTURE")),
            ))

        subtypes = (ship.get("stateroomSubtypes") or {}).items()
        subtype_names = {_entity(k): v.get("name") for k, v in subtypes}
        always_gty = {_entity(k) for k, v in subtypes if v.get("isAlwaysGuaranteed")}
        gratuity = {_type_key(k): v.get("gratuity") for k, v in (ship.get("stateroomTypes") or {}).items()}
        return {
            "sailing_id": _entity(sailing.get("id")),
            "ship": ship.get("name"),
            "product": product.get("name"),
            "sail_date": sailing.get("sailDateFrom"),
            "nights": sailing.get("numberOfNights"),
            "departure_port": ports.get(sailing.get("portFrom")),
            "itinerary": days,
            "subtype_names": subtype_names,
            "always_guaranteed": always_gty,
            "gratuity": gratuity,
            "gratuities_rate": sailing.get("gratuitiesRate"),
            "concierge_gratuities_rate": sailing.get("conciergeGratuitiesRate"),
        }

    def parse_subtypes(self, data: dict) -> list[Stateroom]:
        """Lowest fare of every stateroom sub-type (from cruise details), incl. guarantees."""
        info = self.parse_details(data)
        names = info.get("subtype_names") or {}
        always_gty = info.get("always_guaranteed") or set()
        url = "Disney cruise details (lowest fare per room type)"
        out: list[Stateroom] = []
        mixes = (data.get("sailingAvailabilityResponse") or {}).get("startingFromPriceByPartyMix") or []
        for mix in mixes[:1]:
            for st_type in mix.get("stateroomTypes") or []:
                tkey = _type_key(st_type.get("id"))
                for sub in st_type.get("stateroomSubTypes") or []:
                    sfp = sub.get("startingFromPrice") or {}
                    sub_id = _entity(sub.get("id"))
                    code = _entity(sfp.get("stateroomCategory")).split("-", 1)[-1] or None
                    fare, tax = fare_per_person(sfp.get("price"))
                    guarantee = (sub_id in always_gty or sfp.get("stateroomId") == "GTY"
                                 or sfp.get("offerType") == "IGT_OGT_VGT")
                    name = names.get(sub_id) or sub_id
                    if guarantee and sub_id not in always_gty:
                        name = f"{name} (guarantee)"
                    out.append(Stateroom(
                        category=TYPE_CATEGORY.get(tkey, "Other"),
                        name=name,
                        code=code,
                        price_per_person=fare,
                        taxes_fees_per_person=tax,
                        sold_out=fare is None,
                        notes=("Guarantee: Disney assigns the stateroom (and its location) before sailing."
                               if guarantee else "Lowest fare for this room type."),
                        source=url,
                    ))
        return out

    @staticmethod
    def map_categories(data: dict, source: str, req: ResearchRequest) -> list[Stateroom]:
        out: list[Stateroom] = []
        odd_party = req.children or req.adults != 2
        for tkey_full, st_type in (data.get("stateroomTypes") or {}).items():
            tkey = _type_key(st_type.get("stateroomType") or tkey_full)
            for sub in (st_type.get("stateroomSubtypes") or {}).values():
                for cat in sub.get("stateroomCategories") or []:
                    code = cat.get("stateroomCategory") or cat.get("bookingCode")
                    if not code:
                        continue
                    price = cat.get("price")
                    fare, tax = fare_per_person(price)
                    notes = [sub.get("name")] if sub.get("name") and sub.get("name") != cat.get("name") else []
                    if cat.get("isAccessible"):
                        notes.append("Accessible")
                    if fare is not None and cat.get("stateroomCount"):
                        n = cat["stateroomCount"]
                        notes.append(f"{n} stateroom{'s' if n != 1 else ''} available")
                    total = party_total(price)
                    if fare is not None and odd_party and total:
                        notes.append(f"Party total ${total:,.2f} incl. taxes")
                    out.append(Stateroom(
                        category=TYPE_CATEGORY.get(tkey, "Other"),
                        name=cat.get("name") or sub.get("name") or code,
                        code=code,
                        price_per_person=fare,
                        taxes_fees_per_person=tax,
                        sold_out=fare is None,
                        notes="; ".join(notes) or None,
                        source=source,
                    ))
        return out

    @staticmethod
    def class_leadins(sailing: dict, source: str) -> list[Stateroom]:
        out = []
        for tp in (sailing.get("travelParties") or {}).get("0") or []:
            tkey = _type_key(tp.get("stateroomType"))
            fare, tax = fare_per_person(tp.get("price")) if tp.get("price") else (None, None)
            out.append(Stateroom(
                category=TYPE_CATEGORY.get(tkey, "Other"),
                name=f"{TYPE_CATEGORY.get(tkey, tkey.title())} (lowest fare)",
                code=tp.get("stateroomCategory"),
                price_per_person=fare,
                taxes_fees_per_person=tax,
                sold_out=fare is None,
                notes="Lowest fare in this stateroom class.",
                source=source,
            ))
        return out

    @staticmethod
    def gratuity_addons(info: dict, source: str) -> list[AddOn]:
        grat = info.get("gratuity") or {}
        std = grat.get("INSIDE") or grat.get("VERANDAH")
        con = grat.get("SUITE")
        out = []
        if std:
            out.append(AddOn(
                kind="other", name="Crew gratuities", price=float(std), price_unit="per_person_per_day",
                unit_label="per person, per night", port=None,
                description=(f"Suggested gratuities for dining and stateroom crew (${info['gratuities_rate']:g} per person "
                             f"for this sailing); not included in the cruise fare."
                             if info.get("gratuities_rate") else "Suggested crew gratuities; not included in the cruise fare."),
                source=source))
        if con and con != std:
            out.append(AddOn(
                kind="other", name="Crew gratuities (Concierge)", price=float(con), price_unit="per_person_per_day",
                unit_label="per person, per night", port=None,
                description=(f"Concierge-level gratuities (${info['concierge_gratuities_rate']:g} per person for this sailing)."
                             if info.get("concierge_gratuities_rate") else "Concierge-level gratuities."),
                source=source))
        return out

    # ── Catalog ──────────────────────────────────────────────────────────

    def iter_catalog(self, today: Optional[date] = None, workers: int = WORKERS) -> Iterator[CatalogSailing]:
        """Every bookable Disney sailing with lead-in fares per class (2 adults, fare excl. taxes).

        ~35 search pages (5 cards each, sorted by date — the "recommended" order isn't
        stable across pages) give every product+itinerary; one available-sailings call
        per itinerary (~150) gives the dates and prices.
        """
        today_s = (today or date.today()).isoformat()
        pages = self.product_pages([], max_pages=200, workers=workers)
        expected = int((pages[0] if pages else {}).get("totalAvailableCruises") or 0)
        meta: dict[tuple, dict] = {}
        listed: set[str] = set()
        for page in pages:
            for prod in page.get("products") or []:
                for itin in prod.get("itineraries") or []:
                    key = (prod.get("productId"), itin.get("itineraryId"))
                    meta.setdefault(key, {"name": prod.get("productName") or prod.get("productDisplayName") or "",
                                          "ports": itin.get("portsOfCall") or []})
                    listed.update(s.get("sailingId") for s in itin.get("sailings") or [])
        keys = list(meta)
        results = self._post_many([self._sailings_call(p, i, 2, 0) for p, i in keys], workers=workers)
        seen: set[str] = set()
        for key, res in zip(keys, results):
            for s in (res or {}).get("sailings") or []:
                sid = s.get("sailingId")
                if not sid or sid in seen or (s.get("sailDateFrom") or "") < today_s:
                    continue
                seen.add(sid)
                yield self.catalog_row(s, meta[key])
        missing = listed - seen
        if missing or (expected and len(seen) < expected):
            log.warning("Disney catalog: %d sailings yielded, search lists %d (%d not dated)",
                        len(seen), expected, len(missing))

    def catalog_row(self, s: dict, meta: dict) -> CatalogSailing:
        ship = s.get("ship") or {}
        prices: dict[str, Optional[float]] = {}
        tax = None
        for tp in (s.get("travelParties") or {}).get("0") or []:
            label = TYPE_CATEGORY.get(_type_key(tp.get("stateroomType")))
            if not label:
                continue
            fare, t = fare_per_person(tp.get("price")) if tp.get("price") and tp.get("available", True) else (None, None)
            if fare is not None and (prices.get(label) is None or fare < prices[label]):
                prices[label] = fare
            else:
                prices.setdefault(label, None)
            tax = tax if tax is not None else t
        ship_name = ship.get("name") or SHIP_NAMES.get(ship.get("seawareId"), "")
        return CatalogSailing(
            cruise_line=self.cruise_line,
            sailing_key=s["sailingId"],
            ship=ship_name,
            sail_date=s["sailDateFrom"],
            nights=s.get("numberOfNights"),
            ship_code=ship.get("seawareId"),
            itinerary_name=meta.get("name") or None,
            departure_port=departure_from_name(meta.get("name") or ""),
            ports=list(meta.get("ports") or []),
            booking_url=booking_url(s["sailingId"], meta.get("name") or "", s["sailDateFrom"], ship_name),
            prices=dict(sorted(prices.items(), key=lambda kv: list(TYPE_CATEGORY.values()).index(kv[0]))),
            taxes_fees_per_person=tax,
            currency="USD",
        )

    # ── API calls ────────────────────────────────────────────────────────

    @staticmethod
    def _products_call(filters: list[str], page: int, adults: int, children: int) -> dict:
        return {"method": "POST", "path": PA + "available-products/", "auth": False, "body": {
            "currency": "USD", "filters": filters, "partyMix": party_mix(adults, children),
            "region": "INTL", "storeId": "DCL", "affiliations": [], "page": page, "pageHistory": False,
            "includeAdvancedBookingPrices": True, "exploreMorePage": 1, "exploreMorePageHistory": False,
            "sorts": [{"criteria": "DATE", "order": "ASC"}]}}

    @staticmethod
    def _sailings_call(product_id: str, itinerary_id: str, adults: int, children: int,
                       filters: Optional[list[str]] = None) -> dict:
        return {"method": "POST", "path": PA + "available-sailings/", "auth": False, "body": {
            "currency": "USD", "filters": filters or [], "partyMix": party_mix(adults, children),
            "region": "INTL", "storeId": "DCL", "affiliations": [], "itineraryId": itinerary_id,
            "productId": product_id, "includeAdvancedBookingPrices": True}}

    @staticmethod
    def _details_call(sailing_id: str, adults: int, children: int) -> dict:
        return {"method": "POST", "path": SA + f"get-cruise-details-availability/{sailing_id}?region=INTL",
                "auth": True, "body": {
                    "partyMix": party_mix(adults, children, sailing_avail=True), "sailingId": sailing_id,
                    "view": "cruise-details", "affiliations": [], "region": "en-us", "currency": "USD"}}

    @staticmethod
    def _category_call(sailing_id: str, adults: int, children: int) -> dict:
        return {"method": "POST", "path": SA + "stateroom-category-search", "auth": True, "body": {
            "sailingId": sailing_id, "partyMix": party_mix(adults, children, sailing_avail=True),
            "region": "en-us", "currency": "USD", "affiliations": []}}

    def _post_many(self, calls: list[dict], workers: int = WORKERS) -> list[Optional[dict]]:
        """Run API calls directly, or in a real browser once Disney refuses direct requests."""
        if not calls:
            return []
        if not self._use_browser:
            blocked: list[str] = []
            if len(calls) == 1:
                out = [self._direct(calls[0], blocked)]
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    out = list(pool.map(lambda c: self._direct(c, blocked), calls))
            if not blocked or any(o is not None for o in out):
                return out
            if not self.cf.configured:
                raise ProviderError(f"Disney refused the request ({blocked[0]}). Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN.")
            log.warning("Disney refused direct requests (%s); switching to Cloudflare browser", blocked[0])
            self._use_browser = True
        batches = [calls[i:i + BROWSER_BATCH] for i in range(0, len(calls), BROWSER_BATCH)]
        if len(batches) == 1:
            return self._via_browser(batches[0])
        with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as pool:
            parts = list(pool.map(self._via_browser, batches))
        return [o for part in parts for o in part]

    def _direct(self, call: dict, blocked: list[str]) -> Optional[dict]:
        status = None
        for attempt in range(4):
            headers = {**API_HEADERS, "x-correlation-id": str(uuid.uuid4()), "x-conversation-id": str(uuid.uuid4())}
            try:
                if call["path"].startswith(PA):
                    self._authz()
                if call.get("auth"):
                    headers["authorization"] = "BEARER " + self._client_token()
                self.calls += 1
                resp = self.http.request(call["method"], BASE + call["path"], headers=headers,
                                         content=json.dumps(call["body"]) if call.get("body") is not None else None)
            except ProviderError as exc:
                blocked.append(str(exc))
                return None
            except httpx.HTTPError as exc:
                log.info("Disney %s failed: %s", call["path"], exc)
                self._sleep(1 + attempt)
                continue
            status = resp.status_code
            if status == 429 or (status >= 500 and attempt < 2):
                wait = _num(resp.headers.get("retry-after")) or 2 ** attempt
                self._sleep(min(wait, 30))
                continue
            if status == 401 and call.get("auth") and attempt == 0:
                self._token = None  # expired token: fetch a new one and retry once
                continue
            if status in (401, 403) or "queue-it" in str(resp.url):
                blocked.append(f"HTTP {status}")
                return None
            if status != 200:
                log.info("Disney %s → HTTP %s: %s", call["path"], status, resp.text[:200])
                return None
            try:
                return resp.json()
            except ValueError:
                blocked.append("non-JSON response")
                return None
        if status == 429:
            blocked.append("HTTP 429")
        return None

    def _authz(self) -> None:
        if self._authz_done:
            return
        with self._lock:
            if self._authz_done:
                return
            try:
                self.calls += 1
                self.http.post(BASE + PA + "authz/private", content="{}", headers=API_HEADERS)
            except httpx.HTTPError as exc:
                log.info("Disney authz failed: %s", exc)
            self._authz_done = True

    def _client_token(self) -> str:
        with self._lock:
            if self._token and time.time() - self._token_at < 1500:
                return self._token
            try:
                self.calls += 1
                resp = self.http.get(BASE + SA + "client-token/", headers=API_HEADERS)
                token = resp.json().get("access_token") if resp.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                token = None
            if not token:
                raise ProviderError("Disney refused the client token")
            self._token, self._token_at = token, time.time()
            return token

    def _via_browser(self, calls: list[dict]) -> list[Optional[dict]]:
        payload = [{"m": c["method"], "u": c["path"], "b": c.get("body"), "a": bool(c.get("auth"))} for c in calls]
        script = (
            "(async function(){var calls=" + json.dumps(payload) + ";var strip=" + json.dumps(STRIP_KEYS) + ";"
            "function s(o){if(Array.isArray(o))return o.map(s);"
            "if(o&&typeof o==='object'){var r={};for(var k in o){if(strip.indexOf(k)>=0)continue;"
            "if(k==='stateroomTypes'&&o.seawareId)continue;r[k]=s(o[k]);}return r;}return o;}"
            "var H={'accept':'application/json, text/plain, */*','content-type':'application/json',"
            "'x-use-voyage-svc':'true','x-dash-phase-one':'true','x-bypass-product-avail-svc':'false'};"
            "var tok=null;async function t(){if(!tok){var r=await fetch('" + SA + "client-token/',{headers:H});"
            "tok=(await r.json()).access_token;}return tok;}"
            "try{await fetch('" + PA + "authz/private',{method:'POST',headers:H,body:'{}'});}catch(e){}"
            "var out=await Promise.all(calls.map(async function(c){try{var h=Object.assign({},H);"
            "if(c.a)h['authorization']='BEARER '+(await t());"
            "var r=await fetch(c.u,{method:c.m,headers:h,body:c.b?JSON.stringify(c.b):undefined});"
            "return r.ok?s(await r.json()):null;}catch(e){return null;}}));"
            "var d=document.createElement('div');d.id='qp-dcl';"
            "d.setAttribute('data-json',encodeURIComponent(JSON.stringify(out)));document.body.appendChild(d);})();"
        )
        try:
            html = self.cf.content({
                "url": BROWSER_PAGE,
                "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                "addScriptTag": [{"content": script}],
                "waitForSelector": {"selector": "#qp-dcl", "timeout": 55000},
            })
        except BrowserRenderingError as exc:
            raise ProviderError(f"Disney request failed in browser: {exc}") from exc
        self.calls += 1
        node = BeautifulSoup(html, "html.parser").find(id="qp-dcl")
        if node is None:
            raise ProviderError("Disney returned no data from the browser")
        try:
            out = json.loads(unquote(node.get("data-json", "")))
        except ValueError as exc:
            raise ProviderError("Disney returned unreadable data from the browser") from exc
        return out if isinstance(out, list) and len(out) == len(calls) else [None] * len(calls)
