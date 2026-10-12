"""SQLite storage for raw batches, validation reports and clean candles.

- ``raw_batches`` keeps the exact provider bytes and provenance, so any clean candle can be traced back
  and re-validated.
- ``clean_candles`` has one row per (symbol, interval, open_time). A later batch that reports different
  values for a stored candle replaces it, and the replacement is counted as a revision.
- One ingest writes everything in a single transaction.
- Retention: the CoinGecko API Terms (section 6.1, version of 5 Sept 2025) allow cached data only if it is
  refreshed at least every 24 hours, and require deletion of all stored data if API access ends.
  ``purge_expired`` and ``purge_provider`` implement that. Fixture data is ours and is never purged.
  Validation reports (our own pipeline metadata) are kept; provider error snippets in them are short.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from scheme1.contracts import Candle, CandleBatch, DataStatus, Interval, ValidationReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_batches (
    batch_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    payload BLOB NOT NULL,
    batch_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_reports (
    batch_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    error TEXT,
    report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS validation_reports_by_symbol ON validation_reports(symbol, interval, checked_at);
CREATE TABLE IF NOT EXISTS clean_candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);
"""


class CandleStore:
    def __init__(self, path: Path | str = ":memory:"):
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def save_ingest(
        self,
        report: ValidationReport,
        symbol: str,
        interval: Interval,
        *,
        batch: CandleBatch | None = None,
        raw: bytes | None = None,
        clean: tuple[Candle, ...] = (),
        error: str | None = None,
    ) -> int:
        """Store one ingest atomically. Returns how many already stored clean candles were revised."""
        revised = 0
        with self._db:  # commits on success, rolls back on any exception
            if batch is not None:
                if raw is None:
                    raise ValueError("raw payload is required with a batch")
                p = batch.provenance
                self._db.execute(
                    "INSERT INTO raw_batches VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (batch.batch_id, p.provider, p.kind.value, batch.symbol, batch.interval.value,
                     p.retrieved_at.isoformat(), json.dumps(p.request, sort_keys=True), p.content_sha256,
                     raw, batch.model_dump_json()),
                )
            self._db.execute(
                "INSERT INTO validation_reports VALUES (?,?,?,?,?,?,?)",
                (report.batch_id, symbol, interval.value, report.status.value, report.checked_at.isoformat(),
                 error, report.model_dump_json()),
            )
            for c in clean:
                key = (c.symbol, c.interval.value, c.open_time.isoformat())
                values = (str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume))
                old = self._db.execute(
                    "SELECT open, high, low, close, volume FROM clean_candles WHERE symbol=? AND interval=? AND open_time=?",
                    key,
                ).fetchone()
                if old is not None and tuple(Decimal(x) for x in old) != tuple(Decimal(x) for x in values):
                    revised += 1
                self._db.execute(
                    "INSERT OR REPLACE INTO clean_candles VALUES (?,?,?,?,?,?,?,?,?)",
                    (*key, *values, report.batch_id),
                )
        return revised

    def purge_expired(self, now: datetime, max_age: timedelta, exempt: tuple[str, ...] = ("fixture",)) -> int:
        """Delete provider data not (re)retrieved within ``max_age``. Returns the number of raw batches removed.

        A clean candle survives as long as its newest source batch does: re-fetching a candle moves it to the
        new batch, which is how a cache gets "refreshed".
        """
        cutoff = now - max_age
        rows = self._db.execute("SELECT batch_id, provider, retrieved_at FROM raw_batches").fetchall()
        expired = [b for b, provider, t in rows if provider not in exempt and datetime.fromisoformat(t) < cutoff]
        self._delete_batches(expired)
        return len(expired)

    def purge_provider(self, provider: str) -> int:
        """Delete everything stored from one provider (for example when its API access ends)."""
        rows = self._db.execute("SELECT batch_id FROM raw_batches WHERE provider=?", (provider,)).fetchall()
        expired = [r[0] for r in rows]
        self._delete_batches(expired)
        return len(expired)

    def _delete_batches(self, batch_ids: list[str]) -> None:
        with self._db:
            for batch_id in batch_ids:
                self._db.execute("DELETE FROM clean_candles WHERE batch_id=?", (batch_id,))
                self._db.execute("DELETE FROM raw_batches WHERE batch_id=?", (batch_id,))
        if batch_ids:
            self._db.execute("VACUUM")  # also drop the deleted pages from the file

    def clean_candles(self, symbol: str, interval: Interval, *, start: datetime | None = None,
                      end: datetime | None = None, limit: int | None = None) -> tuple[Candle, ...]:
        """Clean candles with ``start <= open_time < end``, oldest first. ``limit`` keeps the newest ones."""
        return tuple(c for c, _ in self.clean_candles_with_batches(symbol, interval, start=start, end=end, limit=limit))

    def clean_candles_with_batches(self, symbol: str, interval: Interval, *, start: datetime | None = None,
                                   end: datetime | None = None, limit: int | None = None) -> tuple[tuple[Candle, str], ...]:
        """Like ``clean_candles``, paired with the id of the batch each candle was last stored from."""
        sql = "SELECT open_time, open, high, low, close, volume, batch_id FROM clean_candles WHERE symbol=? AND interval=?"
        args: list[object] = [symbol, interval.value]
        if start is not None:
            sql += " AND open_time >= ?"
            args.append(start.isoformat())
        if end is not None:
            sql += " AND open_time < ?"
            args.append(end.isoformat())
        sql += " ORDER BY open_time DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self._db.execute(sql, args).fetchall()
        return tuple(
            (Candle(symbol=symbol, interval=interval, open_time=datetime.fromisoformat(t), open=Decimal(o),
                    high=Decimal(h), low=Decimal(low), close=Decimal(c), volume=Decimal(v)), batch_id)
            for t, o, h, low, c, v, batch_id in reversed(rows)
        )

    def latest_clean_before(self, symbol: str, interval: Interval, before: datetime) -> Candle | None:
        found = self.clean_candles(symbol, interval, end=before, limit=1)
        return found[0] if found else None

    def latest_report(self, symbol: str, interval: Interval) -> tuple[ValidationReport, str | None] | None:
        row = self._db.execute(
            "SELECT report_json, error FROM validation_reports WHERE symbol=? AND interval=? "
            "ORDER BY checked_at DESC, rowid DESC LIMIT 1",
            (symbol, interval.value),
        ).fetchone()
        return (ValidationReport.model_validate_json(row[0]), row[1]) if row else None

    def raw_payload(self, batch_id: str) -> bytes | None:
        row = self._db.execute("SELECT payload FROM raw_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return row[0] if row else None

    def statuses(self, symbol: str, interval: Interval) -> list[DataStatus]:
        rows = self._db.execute(
            "SELECT status FROM validation_reports WHERE symbol=? AND interval=? ORDER BY checked_at, rowid",
            (symbol, interval.value),
        ).fetchall()
        return [DataStatus(r[0]) for r in rows]
