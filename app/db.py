"""Tiny SQLite store: one row per quote, the quote itself kept as JSON."""

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import Quote

_lock = threading.Lock()


class QuoteStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "quotes.db"
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, created_at TEXT, body TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save(self, quote: Quote) -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO quotes (id, created_at, body) VALUES (?, ?, ?)",
                (quote.id, quote.created_at, quote.model_dump_json()),
            )

    def get(self, quote_id: str) -> Optional[Quote]:
        with self._connect() as conn:
            row = conn.execute("SELECT body FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return Quote.model_validate_json(row[0]) if row else None

    def list(self, limit: int = 100) -> list[Quote]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT body FROM quotes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Quote.model_validate_json(r[0]) for r in rows]

    def delete(self, quote_id: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
