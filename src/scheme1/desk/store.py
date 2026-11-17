"""SQLite storage for the paper desk: a hash-chained, append-only audit log plus the working state.

Tables
- ``audit_log``: every record the desk produces, in order: desk events, decisions (slim), order intents,
  risk verdicts, order versions, fills and portfolio snapshots. Each row stores
  ``sha256(prev_hash | seq | at | kind | record_id | payload)``, so editing or deleting any row breaks the
  chain and ``verify_chain()`` reports where. Triggers refuse UPDATE and DELETE on it.
- ``decision_details``: the full ``Decision`` (feature values, agent evidence), purged after 24 hours.
- ``desk_state``: what the desk needs to resume after a restart (flags, marks, protective levels, equity
  peaks), overwritten in place.

Writes are only allowed inside ``transaction()``, so one desk step is stored completely or not at all.

What is kept for good follows docs/STAGE3_DATA.md section 1a, item 8: our own outputs (signals, risk
verdicts, orders, fills, equity) stay. Values copied from GeckoTerminal (feature values, candle closes used
as marks) are kept at most 24 hours, like the candles themselves (D11).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel

from scheme1.contracts import Decision, Fill, Order

GENESIS = "0" * 64
DETAILS_RETENTION = timedelta(hours=24)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    symbol TEXT,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_by_kind ON audit_log(kind, seq);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE IF NOT EXISTS decision_details (
    decision_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS desk_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _hash(prev: str, seq: int, at: str, kind: str, record_id: str, payload: str) -> str:
    return hashlib.sha256("|".join((prev, str(seq), at, kind, record_id, payload)).encode()).hexdigest()


def slim_decision(d: Decision) -> dict[str, Any]:
    """A decision without the values copied or derived from provider data (kept for good)."""
    return {
        "decision_id": d.decision_id, "created_at": d.created_at.isoformat(), "symbol": d.symbol,
        "interval": d.interval.value, "signal": d.signal.value, "strategy_version": d.strategy_version,
        "model_version": d.model_version, "feature_set_version": d.features.feature_set_version,
        "features_as_of": d.features.as_of.isoformat(), "data_status": d.data_status.value,
        "data_age_seconds": d.data_age_seconds, "confidence": d.confidence.value,
        "confidence_kind": d.confidence.kind, "explanation": d.explanation,
        "rejection_reasons": list(d.rejection_reasons),
        "agents": [{"agent_id": o.agent_id, "role": o.role, "status": o.status.value, "stance": o.stance.value,
                    "score": o.score, "error": o.error} for o in d.agent_outputs],
    }


class DeskStore:
    def __init__(self, path: Path | str = ":memory:"):
        self._db = sqlite3.connect(str(path), isolation_level=None)  # transactions are explicit
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._in_tx = False

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._in_tx:
            raise RuntimeError("transactions don't nest")
        self._db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
        finally:
            self._in_tx = False

    def _need_tx(self) -> None:
        if not self._in_tx:
            raise RuntimeError("desk writes must happen inside store.transaction()")

    # -------------------------------------------------------------- audit log

    def append(self, kind: str, record_id: str, at: datetime, payload: BaseModel | dict[str, Any],
               symbol: str | None = None) -> int:
        self._need_tx()
        text = (payload.model_dump_json() if isinstance(payload, BaseModel)
                else json.dumps(payload, sort_keys=True, default=str))
        row = self._db.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
        at_s = at.isoformat()
        self._db.execute(
            "INSERT INTO audit_log (seq, at, kind, record_id, symbol, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
            (seq, at_s, kind, record_id, symbol, text, prev, _hash(prev, seq, at_s, kind, record_id, text)))
        return seq

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Payloads in the order they were written, each with ``_seq``, ``_at`` and ``_kind`` added."""
        sql = "SELECT seq, at, kind, payload FROM audit_log" + (" WHERE kind = ?" if kind else "") + " ORDER BY seq"
        rows = self._db.execute(sql, (kind,) if kind else ()).fetchall()
        return [{**json.loads(p), "_seq": s, "_at": a, "_kind": k} for s, a, k, p in rows]

    def fills(self) -> list[Fill]:
        return [Fill.model_validate(_clean(r)) for r in self.records("fill")]

    def orders(self) -> dict[str, Order]:
        """The latest version of every order."""
        latest: dict[str, Order] = {}
        for r in self.records("order"):
            order = Order.model_validate(_clean(r))
            latest[order.order_id] = order
        return latest

    def verify_chain(self) -> tuple[bool, str]:
        prev, expected = GENESIS, 1
        rows = self._db.execute(
            "SELECT seq, at, kind, record_id, payload, prev_hash, hash FROM audit_log ORDER BY seq").fetchall()
        for seq, at, kind, record_id, payload, prev_hash, stored in rows:
            if seq != expected:
                return False, f"record {expected} is missing"
            if prev_hash != prev:
                return False, f"record {seq} doesn't follow record {seq - 1}"
            if _hash(prev, seq, at, kind, record_id, payload) != stored:
                return False, f"record {seq} ({kind}) was changed after it was written"
            prev, expected = stored, expected + 1
        return True, f"{len(rows)} records, chain intact"

    # ------------------------------------------------------- decision details

    def save_decision_details(self, decision: Decision) -> None:
        self._need_tx()
        self._db.execute("INSERT OR REPLACE INTO decision_details VALUES (?,?,?)",
                         (decision.decision_id, decision.created_at.isoformat(), decision.model_dump_json()))

    def decision_details(self, decision_id: str) -> Decision | None:
        row = self._db.execute("SELECT payload FROM decision_details WHERE decision_id = ?", (decision_id,)).fetchone()
        return Decision.model_validate_json(row[0]) if row else None

    def purge_details(self, now: datetime, max_age: timedelta = DETAILS_RETENTION) -> int:
        self._need_tx()
        cur = self._db.execute("DELETE FROM decision_details WHERE created_at < ?", ((now - max_age).isoformat(),))
        return cur.rowcount

    # ------------------------------------------------------------ desk state

    def put_state(self, key: str, value: Any) -> None:
        self._need_tx()
        self._db.execute("INSERT OR REPLACE INTO desk_state VALUES (?,?)", (key, json.dumps(value, default=str)))

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}
