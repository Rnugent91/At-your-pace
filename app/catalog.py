"""Master cruise catalog: every sailing of every line, with daily price history.

`sync_line()` pulls a line's whole catalog (lead-in fare per room class) and
records a price point only when a fare changes, so history stays small even
when every sailing is refreshed daily. Watched sailings also get individual
room-type prices (from the provider's `research()`), tracked the same way.
"""

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import SailingResearch
from .providers.base import CatalogSailing

log = logging.getLogger(__name__)
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sailings (
  line TEXT NOT NULL, sailing_key TEXT NOT NULL,
  ship TEXT, ship_code TEXT, sail_date TEXT, nights INTEGER,
  itinerary_name TEXT, departure_port TEXT, ports TEXT, booking_url TEXT, currency TEXT,
  taxes_fees REAL,
  prices TEXT,           -- JSON {room class: fare pp or null}
  prev_prices TEXT,      -- JSON, the fares before the latest change
  min_price REAL,        -- cheapest available class fare, for sorting
  price_changed_on TEXT, first_seen TEXT, last_seen TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (line, sailing_key)
);
CREATE INDEX IF NOT EXISTS sailings_date ON sailings (sail_date);
CREATE INDEX IF NOT EXISTS sailings_ship ON sailings (ship);
CREATE TABLE IF NOT EXISTS class_history (
  line TEXT NOT NULL, sailing_key TEXT NOT NULL, room_class TEXT NOT NULL,
  observed_on TEXT NOT NULL, price REAL
);
CREATE INDEX IF NOT EXISTS class_history_key ON class_history (line, sailing_key);
CREATE TABLE IF NOT EXISTS room_history (
  line TEXT NOT NULL, sailing_key TEXT NOT NULL, room TEXT NOT NULL, category TEXT, code TEXT,
  observed_on TEXT NOT NULL, price REAL, taxes_fees REAL
);
CREATE INDEX IF NOT EXISTS room_history_key ON room_history (line, sailing_key);
CREATE TABLE IF NOT EXISTS watches (
  line TEXT NOT NULL, sailing_key TEXT NOT NULL, created_at TEXT, note TEXT,
  rooms_checked_at TEXT, rooms_error TEXT,
  PRIMARY KEY (line, sailing_key)
);
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, line TEXT, started_at TEXT, finished_at TEXT,
  sailings INTEGER, changed INTEGER, removed INTEGER, error TEXT
);
"""

SORTS = {
    "date": "sail_date ASC, min_price ASC",
    "price": "min_price IS NULL, min_price ASC, sail_date ASC",
    "per_night": "min_price IS NULL, min_price * 1.0 / MAX(nights, 1) ASC",
    "drop": "price_changed_on DESC, sail_date ASC",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _min_price(prices: dict) -> Optional[float]:
    vals = [v for v in prices.values() if v is not None]
    return min(vals) if vals else None


@dataclass
class SyncResult:
    line: str
    sailings: int = 0
    changed: int = 0
    removed: int = 0
    error: Optional[str] = None


class CatalogStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "catalog.db"
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Writing ──────────────────────────────────────────────────────────

    def upsert(self, line: str, rows: Iterable[CatalogSailing], today: Optional[str] = None,
               complete: bool = True) -> SyncResult:
        """Store a line's catalog. With `complete`, sailings missing from `rows` are marked inactive."""
        today = today or date.today().isoformat()
        now = _now()
        result = SyncResult(line)
        seen: set[str] = set()
        with _lock, self._connect() as conn:
            existing = {
                r["sailing_key"]: r
                for r in conn.execute("SELECT sailing_key, prices FROM sailings WHERE line = ?", (line,))
            }
            for s in rows:
                if s.sailing_key in seen:
                    continue
                seen.add(s.sailing_key)
                result.sailings += 1
                old = existing.get(s.sailing_key)
                old_prices = json.loads(old["prices"]) if old and old["prices"] else {}
                changed = {k: v for k, v in s.prices.items() if old is None or old_prices.get(k, "missing") != v}
                if changed:
                    conn.executemany(
                        "INSERT INTO class_history (line, sailing_key, room_class, observed_on, price) VALUES (?,?,?,?,?)",
                        [(line, s.sailing_key, k, today, v) for k, v in changed.items()],
                    )
                price_moved = old is not None and bool(changed)
                if price_moved:
                    result.changed += 1
                conn.execute(
                    """INSERT INTO sailings (line, sailing_key, ship, ship_code, sail_date, nights, itinerary_name,
                         departure_port, ports, booking_url, currency, taxes_fees, prices, prev_prices, min_price,
                         price_changed_on, first_seen, last_seen, active)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT (line, sailing_key) DO UPDATE SET
                         ship=excluded.ship, ship_code=excluded.ship_code, sail_date=excluded.sail_date,
                         nights=excluded.nights, itinerary_name=excluded.itinerary_name,
                         departure_port=excluded.departure_port, ports=excluded.ports,
                         booking_url=excluded.booking_url, currency=excluded.currency,
                         taxes_fees=excluded.taxes_fees, prices=excluded.prices, min_price=excluded.min_price,
                         prev_prices=CASE WHEN ? THEN ? ELSE sailings.prev_prices END,
                         price_changed_on=CASE WHEN ? THEN ? ELSE sailings.price_changed_on END,
                         last_seen=excluded.last_seen, active=1""",
                    (
                        line, s.sailing_key, s.ship, s.ship_code, s.sail_date, s.nights, s.itinerary_name,
                        s.departure_port, json.dumps(s.ports), s.booking_url, s.currency, s.taxes_fees_per_person,
                        json.dumps(s.prices), None, _min_price(s.prices), None, now, now,
                        price_moved, json.dumps(old_prices), price_moved, today,
                    ),
                )
            if complete and seen:
                gone = [k for k in existing if k not in seen]
                conn.executemany(
                    "UPDATE sailings SET active = 0 WHERE line = ? AND sailing_key = ?", [(line, k) for k in gone]
                )
                result.removed = len(gone)
        return result

    def record_rooms(self, line: str, sailing_key: str, research: SailingResearch,
                     today: Optional[str] = None) -> int:
        """Record individual room-type prices for a sailing; returns how many changed."""
        today = today or date.today().isoformat()
        latest = {r["room"]: r["price"] for r in self.latest_rooms(line, sailing_key)}
        rows = []
        for room in research.staterooms:
            name = room.name if not room.code or room.code in room.name else f"{room.name} ({room.code})"
            if name not in latest or latest[name] != room.price_per_person:
                rows.append((line, sailing_key, name, room.category, room.code, today,
                             room.price_per_person, room.taxes_fees_per_person))
        with _lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO room_history (line, sailing_key, room, category, code, observed_on, price, taxes_fees)"
                " VALUES (?,?,?,?,?,?,?,?)", rows,
            )
            conn.execute(
                "UPDATE watches SET rooms_checked_at = ?, rooms_error = NULL WHERE line = ? AND sailing_key = ?",
                (_now(), line, sailing_key),
            )
        return len(rows)

    def record_rooms_error(self, line: str, sailing_key: str, error: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "UPDATE watches SET rooms_checked_at = ?, rooms_error = ? WHERE line = ? AND sailing_key = ?",
                (_now(), error[:500], line, sailing_key),
            )

    def log_run(self, result: SyncResult, started_at: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_runs (line, started_at, finished_at, sailings, changed, removed, error)"
                " VALUES (?,?,?,?,?,?,?)",
                (result.line, started_at, _now(), result.sailings, result.changed, result.removed, result.error),
            )

    def watch(self, line: str, sailing_key: str, note: str = "") -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watches (line, sailing_key, created_at, note) VALUES (?,?,?,?)",
                (line, sailing_key, _now(), note),
            )

    def unwatch(self, line: str, sailing_key: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute("DELETE FROM watches WHERE line = ? AND sailing_key = ?", (line, sailing_key))

    # ── Reading ──────────────────────────────────────────────────────────

    def search(self, *, line: str = "", ship: str = "", port: str = "", date_from: str = "", date_to: str = "",
               nights_min: Optional[int] = None, nights_max: Optional[int] = None, room_class: str = "",
               max_price: Optional[float] = None, dropped_only: bool = False, watched_only: bool = False,
               sort: str = "date", limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
        where, args = ["s.active = 1", "s.sail_date >= ?"], [date.today().isoformat()]
        if line:
            where.append("s.line = ?"); args.append(line)
        if ship:
            where.append("(s.ship LIKE ? OR s.ship_code = ?)"); args += [f"%{ship}%", ship.upper()]
        if port:
            where.append("(s.departure_port LIKE ? OR s.ports LIKE ? OR s.itinerary_name LIKE ?)")
            args += [f"%{port}%"] * 3
        if date_from:
            where.append("s.sail_date >= ?"); args.append(date_from)
        if date_to:
            where.append("s.sail_date <= ?"); args.append(date_to)
        if nights_min is not None:
            where.append("s.nights >= ?"); args.append(nights_min)
        if nights_max is not None:
            where.append("s.nights <= ?"); args.append(nights_max)
        price_expr = "s.min_price"
        if room_class:
            price_expr = "CAST(json_extract(s.prices, '$.\"' || ? || '\"') AS REAL)"
            args_class = [room_class]
            where.append(f"{price_expr} IS NOT NULL"); args += args_class
        if max_price is not None:
            where.append(f"{price_expr} <= ?")
            if room_class:
                args.append(room_class)
            args.append(max_price)
        if dropped_only:
            where.append("s.prev_prices IS NOT NULL AND s.price_changed_on IS NOT NULL")
        if watched_only:
            where.append("w.sailing_key IS NOT NULL")
        order = SORTS.get(sort, SORTS["date"])
        sql_from = (" FROM sailings s LEFT JOIN watches w ON w.line = s.line AND w.sailing_key = s.sailing_key"
                    " WHERE " + " AND ".join(where))
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*)" + sql_from, args).fetchone()[0]
            rows = conn.execute(
                "SELECT s.*, w.sailing_key IS NOT NULL AS watched" + sql_from
                + f" ORDER BY {order.replace('min_price', 's.min_price')} LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
        out = [self._row(r) for r in rows]
        if dropped_only:
            out = [r for r in out if r["drop"]]
        return out, total

    def get(self, line: str, sailing_key: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT s.*, w.sailing_key IS NOT NULL AS watched, w.rooms_checked_at, w.rooms_error"
                " FROM sailings s LEFT JOIN watches w ON w.line = s.line AND w.sailing_key = s.sailing_key"
                " WHERE s.line = ? AND s.sailing_key = ?",
                (line, sailing_key),
            ).fetchone()
        return self._row(r) if r else None

    def class_history(self, line: str, sailing_key: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT room_class, observed_on, price FROM class_history WHERE line = ? AND sailing_key = ?"
                " ORDER BY observed_on, rowid",
                (line, sailing_key),
            ).fetchall()
        return [dict(r) for r in rows]

    def room_history(self, line: str, sailing_key: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT room, category, code, observed_on, price, taxes_fees FROM room_history"
                " WHERE line = ? AND sailing_key = ? ORDER BY observed_on, rowid",
                (line, sailing_key),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_rooms(self, line: str, sailing_key: str) -> list[dict]:
        """Current price of each room type, with the previous price if it changed."""
        by_room: dict[str, dict] = {}
        for h in self.room_history(line, sailing_key):
            cur = by_room.get(h["room"])
            by_room[h["room"]] = {**h, "prev_price": cur["price"] if cur else None,
                                  "first_price": cur["first_price"] if cur else h["price"]}
        order = {"Interior": 0, "Ocean View": 1, "Balcony": 2, "Suite": 3}
        return sorted(by_room.values(), key=lambda r: (order.get(r["category"], 4), r["price"] is None, r["price"] or 0))

    def active_sailings(self, line: str, date_from: str = "", date_to: str = "") -> list[dict]:
        """Future, still-listed sailings of a line (for full room-type refreshes)."""
        where, args = ["line = ?", "active = 1", "sail_date >= ?"], [line, date_from or date.today().isoformat()]
        if date_to:
            where.append("sail_date <= ?"); args.append(date_to)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT line, sailing_key, sail_date FROM sailings WHERE " + " AND ".join(where) + " ORDER BY sail_date",
                args,
            ).fetchall()
        return [dict(r) for r in rows]

    def room_classes(self) -> list[str]:
        standard = ["Interior", "Ocean View", "Balcony", "Suite"]
        with self._connect() as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT DISTINCT j.key FROM sailings, json_each(sailings.prices) j WHERE sailings.active = 1")]
        return standard + sorted(k for k in keys if k not in standard)

    def watched(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, 1 AS watched, w.rooms_checked_at, w.rooms_error FROM watches w"
                " JOIN sailings s ON s.line = w.line AND s.sailing_key = w.sailing_key ORDER BY s.sail_date"
            ).fetchall()
        return [self._row(r) for r in rows]

    def find(self, line: str, ship: str, sail_date: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT sailing_key FROM sailings WHERE line = ? AND sail_date = ? AND (ship LIKE ? OR ship_code = ?)"
                " ORDER BY active DESC LIMIT 1",
                (line, sail_date, f"%{ship}%", ship.upper()),
            ).fetchone()
        return self.get(line, r["sailing_key"]) if r else None

    def stats(self) -> list[dict]:
        with self._connect() as conn:
            lines = conn.execute(
                "SELECT line, COUNT(*) AS sailings, SUM(active) AS active, MAX(last_seen) AS last_seen,"
                " SUM(CASE WHEN active = 1 AND price_changed_on = date('now') THEN 1 ELSE 0 END) AS changed_today"
                " FROM sailings GROUP BY line ORDER BY line"
            ).fetchall()
            runs = {
                r["line"]: dict(r)
                for r in conn.execute(
                    "SELECT * FROM sync_runs WHERE id IN (SELECT MAX(id) FROM sync_runs GROUP BY line)"
                )
            }
        return [{**dict(r), "last_run": runs.get(r["line"])} for r in lines] + [
            {"line": k, "sailings": 0, "active": 0, "last_seen": None, "changed_today": 0, "last_run": v}
            for k, v in runs.items() if k not in {r["line"] for r in lines}
        ]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["ports"] = json.loads(d.get("ports") or "[]")
        d["prices"] = json.loads(d.get("prices") or "{}")
        d["prev_prices"] = json.loads(d["prev_prices"]) if d.get("prev_prices") else {}
        # Per-class change vs the previous recorded fares (negative = cheaper now)
        d["deltas"] = {
            k: round(v - d["prev_prices"][k], 2)
            for k, v in d["prices"].items()
            if v is not None and d["prev_prices"].get(k) is not None and v != d["prev_prices"][k]
        }
        d["drop"] = min(d["deltas"].values()) if d["deltas"] and min(d["deltas"].values()) < 0 else None
        return d
