"""Daily catalog sync: every sailing of every line, plus room-type prices for watched sailings.

Run by the quotes-sync systemd timer (deploy/quotes-sync.timer), or by hand:

    python -m app.sync                      # all lines, then watched sailings' room types
    python -m app.sync --line Carnival      # one line
    python -m app.sync --rooms-only         # just refresh watched sailings' room types
    python -m app.sync --all-rooms "Royal Caribbean" --room-workers 10
                                            # every room type of every sailing of a line
                                            # (uses Cloudflare for lines that need it — paid plan)
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from .catalog import CatalogStore, SyncResult
from .config import Settings, settings as default_settings
from .providers.base import ResearchRequest

log = logging.getLogger(__name__)


def build_providers(settings: Settings) -> list:
    """Every direct provider, built the same way as the web app's."""
    from .providers import build_direct_providers

    return build_direct_providers(settings)


def catalog_providers(providers: list) -> list:
    """Providers that can list a whole line, one per cruise line (first wins)."""
    out, lines = [], set()
    for p in providers:
        line = getattr(p, "cruise_line", None)
        if hasattr(p, "iter_catalog") and line and line not in lines:
            lines.add(line)
            out.append(p)
    return out


def sync_line(store: CatalogStore, provider) -> SyncResult:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        result = store.upsert(provider.cruise_line, provider.iter_catalog())
    except Exception as exc:  # one line failing must not stop the others
        log.exception("Catalog sync failed for %s", provider.cruise_line)
        result = SyncResult(provider.cruise_line, error=f"{type(exc).__name__}: {exc}"[:500])
    store.log_run(result, started)
    log.info("%s: %d sailings, %d price changes, %d gone%s", result.line, result.sailings, result.changed,
             result.removed, f" — ERROR {result.error}" if result.error else "")
    return result


def sync_catalog(store: CatalogStore, providers: list, only: Optional[str] = None,
                 workers: int = 4) -> list[SyncResult]:
    """Sync lines in parallel (each line talks to a different site, so they don't compete)."""
    chosen = [p for p in catalog_providers(providers) if not only or p.cruise_line.lower() == only.lower()]
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chosen)))) as pool:
        for fut in as_completed([pool.submit(sync_line, store, p) for p in chosen]):
            results.append(fut.result())
    return results


def refresh_rooms(store: CatalogStore, providers: list, line: str, sailing_key: str) -> int:
    """Individual room-type prices for one sailing, via the line's research(). Returns rooms changed."""
    s = store.get(line, sailing_key)
    if s is None:
        raise ValueError(f"Unknown sailing {line} {sailing_key}")
    provider = next((p for p in providers if getattr(p, "cruise_line", None) == line), None)
    if provider is None:
        raise ValueError(f"No live provider for {line}")
    req = ResearchRequest(cruise_line=line, ship=s["ship"] or s["ship_code"], sail_date=s["sail_date"],
                          booking_url=s["booking_url"] or "")
    try:
        research = provider.research(req)
    except Exception as exc:
        store.record_rooms_error(line, sailing_key, f"{type(exc).__name__}: {exc}")
        raise
    return store.record_rooms(line, sailing_key, research)


def refresh_watched(store: CatalogStore, providers: list, workers: int = 4) -> int:
    watched = store.watched()

    def one(s: dict) -> int:
        try:
            return refresh_rooms(store, providers, s["line"], s["sailing_key"])
        except Exception:
            log.exception("Room refresh failed for %s %s", s["line"], s["sailing_key"])
            return 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        changed = sum(pool.map(one, watched))
    log.info("Watched sailings: %d refreshed, %d room prices changed", len(watched), changed)
    return changed


def refresh_line_rooms(store: CatalogStore, providers: list, line: str, workers: int = 8,
                       date_to: str = "") -> tuple[int, int, int]:
    """Room-type prices for every future sailing of a line. Returns (sailings, rooms changed, failures)."""
    sailings = store.active_sailings(line, date_to=date_to)

    def one(s: dict) -> tuple[int, int]:
        try:
            return refresh_rooms(store, providers, line, s["sailing_key"]), 0
        except Exception as exc:
            log.warning("Rooms failed for %s %s: %s", line, s["sailing_key"], exc)
            return 0, 1

    changed = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, (c, f) in enumerate(pool.map(one, sailings), 1):
            changed, failed = changed + c, failed + f
            if i % 100 == 0:
                log.info("%s rooms: %d/%d sailings, %d changes, %d failed", line, i, len(sailings), changed, failed)
    log.info("%s rooms: %d sailings, %d room prices changed, %d failed", line, len(sailings), changed, failed)
    return len(sailings), changed, failed


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--line", help="Only this cruise line (e.g. 'Carnival')")
    ap.add_argument("--rooms-only", action="store_true", help="Only refresh watched sailings' room types")
    ap.add_argument("--no-rooms", action="store_true", help="Skip the watched sailings' room-type refresh")
    ap.add_argument("--all-rooms", action="append", default=[], metavar="LINE",
                    help="Also re-price every room type on every sailing of LINE (repeatable, or 'all')")
    ap.add_argument("--room-workers", type=int, default=8, help="Sailings priced at once for --all-rooms")
    ap.add_argument("--rooms-until", default="", help="Only sailings up to this date for --all-rooms (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = default_settings
    store = CatalogStore(settings.data_dir)
    providers = build_providers(settings)
    failed = False
    if not args.rooms_only:
        failed = any(r.error for r in sync_catalog(store, providers, args.line))
    if not args.no_rooms:
        refresh_watched(store, providers)
    lines = [p.cruise_line for p in catalog_providers(providers)]
    for line in args.all_rooms:
        for name in (lines if line.lower() == "all" else [line]):
            refresh_line_rooms(store, providers, name, args.room_workers, args.rooms_until)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
