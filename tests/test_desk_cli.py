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
