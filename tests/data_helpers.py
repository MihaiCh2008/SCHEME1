"""Shared fakes for data tests: a scripted transport and a clock that records sleeps instead of sleeping."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from scheme1.contracts import Candle, CandleBatch, DataKind, Interval, Provenance
from scheme1.data.http import HttpResponse


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class ScriptedTransport:
    """Returns (or raises) the scripted items in order and records every URL requested."""

    def __init__(self, *script: HttpResponse | Exception):
        self.script = list(script)
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.bodies: list[bytes | None] = []

    def get(self, url: str, headers: dict[str, str], timeout_s: float) -> HttpResponse:
        return self.post(url, None, headers, timeout_s)

    def post(self, url: str, body: bytes | None, headers: dict[str, str], timeout_s: float) -> HttpResponse:
        self.urls.append(url)
        self.headers.append(headers)
        self.bodies.append(body)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SYMBOL = "solana:Fixture111111111111111111111111111111111"  # same as tests/fixtures/fixture_pool_5m.json


def make_candle(open_time: datetime, price: str = "1", interval: Interval = Interval.M5, closed: bool = True,
                symbol: str = SYMBOL, **kw) -> Candle:
    p = Decimal(price)
    base = dict(symbol=symbol, interval=interval, open_time=open_time, open=p, high=p * Decimal("1.01"),
                low=p * Decimal("0.99"), close=p, volume=Decimal("10"), closed=closed)
    base.update(kw)
    return Candle(**base)


def make_batch(candles, interval: Interval = Interval.M5, retrieved_at: datetime | None = None, **kw) -> CandleBatch:
    retrieved_at = retrieved_at or (candles[-1].open_time + timedelta(hours=1) if candles else datetime.now())
    prov = Provenance(provider="test", kind=DataKind.FIXTURE, retrieved_at=retrieved_at, content_sha256="0" * 64)
    return CandleBatch(symbol=SYMBOL, interval=interval, provenance=prov, candles=tuple(candles), **kw)
