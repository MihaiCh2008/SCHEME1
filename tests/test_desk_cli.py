from datetime import timedelta

from scheme1.contracts import Interval, Side, Signal
from scheme1.data.providers.base import FetchResult
from scheme1.data.store import CandleStore
from scheme1.desk.cli import describe, main, run_step
from scheme1.desk.desk import PaperDesk
from scheme1.desk.store import DeskStore
from data_helpers import SYMBOL, make_batch
from test_desk import SAFE, TRADE, T0


class FakeProvider:
    """Serves the candles that have closed by ``now``, plus the one still open, like GeckoTerminal."""

    name = "fake"

    def __init__(self):
        self.now = T0

    def fetch_candles(self, symbol, interval, *, limit=300, end=None):
        shown = [c for c in TRADE if c.open_time < self.now]
        shown = [c if c.close_time <= self.now else c.model_copy(update={"closed": False}) for c in shown][-limit:]
        return FetchResult(batch=make_batch(shown, retrieved_at=self.now), raw=b"[]")


def test_run_step_starts_at_the_newest_candle_and_catches_up_without_deciding_on_old_ones():
    desk, candles, provider = PaperDesk(DeskStore(), now=T0), CandleStore(), FakeProvider()

    def at(i):  # 30 seconds after candle i closed
        provider.now = TRADE[i].close_time + timedelta(seconds=30)
        return run_step(desk, candles, provider, [SYMBOL], Interval.M5, provider.now, lambda sym: SAFE)

    first = at(63)
    assert len(first) == 1 and first[0].candle_open_time == TRADE[63].open_time   # no replay of the past
    assert first[0].decision.signal is Signal.BUY and first[0].verdict.approved
    assert at(63) == []                                                           # nothing new yet
    caught_up = at(65)                                                            # the desk missed candle 64
    assert [r.candle_open_time for r in caught_up] == [TRADE[64].open_time, TRADE[65].open_time]
    assert caught_up[0].decision is None and caught_up[0].fills[0].side is Side.BUY
    assert caught_up[1].decision is not None
    assert any("BUY" in line for line in describe(caught_up[0]))


def test_status_and_controls_from_the_command_line(tmp_path, capsys):
    db = str(tmp_path / "desk.db")
    assert main(["--db", db, "status"]) == 1 and "no paper desk" in capsys.readouterr().out  # nothing created
    PaperDesk(DeskStore(db), now=T0).store.close()
    assert main(["--db", db, "status"]) == 0
    out = capsys.readouterr().out
    assert "state: normal" in out and "integrity: ok" in out and "trades: 0 closed" in out
    assert main(["--db", db, "kill", "--reason", "test"]) == 0 and "emergency_stop" in capsys.readouterr().out
    assert main(["--db", db, "reset"]) == 0 and "state: normal" in capsys.readouterr().out
    assert main(["--db", db, "verify"]) == 0


# ---------------------------------------------------------------- copy loop

from datetime import datetime, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

from scheme1.contracts import CandleBatch, DataKind, Provenance  # noqa: E402
from scheme1.copytrade import PollResult  # noqa: E402
from scheme1.desk.cli import copy_step, describe_copy  # noqa: E402
from test_copytrade_desk import FOLLOWED, NOW, POOL, market, minute, trade  # noqa: E402


class FakeWatcher:
    def __init__(self, *rounds):
        self.rounds = list(rounds)

    def poll(self, cursors):
        trades = self.rounds.pop(0) if self.rounds else []
        return PollResult(trades=trades, cursors={FOLLOWED: trades[-1].signature if trades else "c0"})


class OneMinuteCandles:
    """Serves the copy pool's 1-minute candles that have closed by ``now``."""

    name = "fake"

    def __init__(self, candles):
        self.candles, self.now = candles, NOW

    def fetch_candles(self, symbol, interval, *, limit=300, end=None):
        shown = [c for c in self.candles if c.close_time <= self.now][-limit:]
        prov = Provenance(provider="fake", kind=DataKind.FIXTURE, retrieved_at=self.now, content_sha256="0" * 64)
        return FetchResult(batch=CandleBatch(symbol=symbol, interval=interval, provenance=prov, candles=tuple(shown)),
                           raw=b"[]")


def test_copy_loop_buys_runs_the_positions_candles_and_sells_when_the_trader_does():
    desk = PaperDesk(DeskStore(), now=NOW)
    looked_up = []

    def resolve(mint):
        looked_up.append(mint)
        return market(), ""

    candles = [minute(i, "0.001", h="0.00105", lo="0.00098") for i in range(0, 4)]
    provider = OneMinuteCandles(candles)
    tiny = trade(sol="0.01", sig="tiny", mint="OtherMint")
    watcher = FakeWatcher([tiny, trade()], [], [trade(Side.SELL, sig="sell", at=NOW + timedelta(minutes=3))])
    store = CandleStore()

    copies, steps, _ = copy_step(desk, watcher, store, provider, resolve, lambda s: None, NOW)
    assert [c.action for c in copies] == ["skipped", "bought"] and looked_up == [trade().mint]  # tiny: no lookups
    assert "not looked up" in copies[0].reasons and desk.cursors == {FOLLOWED: "sig1"}
    assert any("bought" in line for line in describe_copy(copies[1]))

    provider.now = NOW + timedelta(minutes=2, seconds=30)
    _, steps, _ = copy_step(desk, watcher, store, provider, resolve, lambda s: None, provider.now)
    assert [s.candle_open_time.minute for s in steps] == [3, 4, 5]       # every 1-minute candle closed by now
    assert desk.ledgers["copy"].quantity(POOL) > 0                       # no stop hit

    provider.now = NOW + timedelta(minutes=3, seconds=30)
    copies, _, _ = copy_step(desk, watcher, store, provider, resolve, lambda s: Decimal("0.0012"), provider.now)
    assert copies[0].action == "sold" and desk.ledgers["copy"].quantity(POOL) == 0
