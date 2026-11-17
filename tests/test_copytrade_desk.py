from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from scheme1.contracts import (
    DataStatus, DeskState, Interval, OrderReason, SafetyCheck, SafetyReport, SafetyStatus, Side, TraderTrade,
    ValidationReport,
)
from scheme1.copytrade import CopyMarket
from scheme1.desk.desk import CandleInputs, DeskConfig, PaperDesk, RebalanceConfig
from scheme1.desk.store import DeskStore
from data_helpers import make_candle
from test_desk import TRADE, run

NOW = datetime(2026, 11, 17, 21, 3, 37, tzinfo=timezone.utc)
POOL = "solana:CMTxtP44CHMxP92ac6FsfJQoqr7YaTjEc33CvDVrNYMn"
MINT = "DPmHerVsAmpSyRzXrFJqKXkzmUbkWzTmkMUqZAfnpump"
FOLLOWED = "FQpu9hW7C5p7irsXYRDHjtBKFc6ooNWni2TX1S6VHxRz"
OTHER = "DrJ6SnDXkEsPeGdmSs93v5rwWumv5QMvAGSZjAyWSd5o"


def trade(side=Side.BUY, sol="1", wallet=FOLLOWED, mint=MINT, sig="sig1", quote="SOL", at=NOW):
    return TraderTrade(trader="trader4" if wallet == FOLLOWED else "trader3", wallet=wallet, signature=sig,
                       slot=1, block_time=at - timedelta(seconds=20), mint=mint, side=side,
                       token_amount=D(1_000_000), quote=quote, quote_amount=D(sol))


def safety(symbol=POOL, ok=True):
    status = SafetyStatus.PASS if ok else SafetyStatus.FAIL
    return SafetyReport(symbol=symbol, checked_at=NOW, safety_config_version="s1.2.0-copy", tradable=ok,
                        checks=(SafetyCheck(name="mint_authority_revoked", status=status, observed="", limit=""),))


def market(price="0.001", ok=True, symbol=POOL, at=NOW):
    return CopyMarket(symbol=symbol, safety=safety(symbol, ok), price_usd=D(price), priced_at=at)


def minute(i, o, h=None, lo=None, cl=None, symbol=POOL, start=NOW, vol="50000"):
    """1-minute candle i minutes after the one the buy happened in."""
    o = D(str(o))
    h, lo, cl = (D(str(x)) if x is not None else o for x in (h, lo, cl))
    t = start.replace(second=0, microsecond=0) + timedelta(minutes=i)
    return make_candle(t, interval=Interval.M1, symbol=symbol, open=o, high=max(h, o, cl), low=min(lo, o, cl),
                       close=cl, volume=D(vol))


def report(status=DataStatus.OK):
    return ValidationReport(batch_id="b", status=status, candles_in=1, candles_out=1, checked_at=NOW)


def feed(desk, *candles, status=DataStatus.OK):
    return [desk.on_candle(CandleInputs(c, report(status), watched=False), now=c.close_time) for c in candles]


def bought_desk(store=None):
    desk = PaperDesk(store or DeskStore(), now=NOW)
    step = desk.on_trader_trade(trade(), NOW, market())
    assert step.action == "bought", step.reasons
    return desk, step


# ---------------------------------------------------------------- copy buys

def test_a_followed_buy_is_copied_at_once_with_the_copy_sleeves_costs():
    desk, step = bought_desk()
    fill, = step.fills
    assert fill.price == D("0.00101") and fill.fee == (fill.quantity * fill.price / 100).quantize(D("1e-8"), "ROUND_UP")
    assert fill.filled_at == NOW and fill.candle_open_time == NOW.replace(second=0)
    cost = fill.quantity * fill.price + fill.fee
    assert D("23") < cost < D("25")                          # about 5% of the copy sleeve's 500, minus a buffer
    assert desk.ledgers["copy"].quantity(POOL) == fill.quantity and desk.ledgers["bot"].quantity(POOL) == 0
    guard = desk.broker.guard(POOL)
    assert guard.sleeve == "copy" and guard.copied_from == FOLLOWED and guard.levels.take_profit_price is None
    assert guard.levels.stop_price == fill.price * D("0.8") and desk.copy_symbols() == [POOL]
    kinds = [r["_kind"] for r in desk.store.records()]
    assert {"trader_trade", "copy_decision", "intent", "risk_verdict", "order", "fill"} <= set(kinds)


@pytest.mark.parametrize("change,reason", [
    (dict(t=trade(sol="0.05")), "too small"),
    (dict(t=trade(sol="5", quote="USDC")), "too small"),
    (dict(m=market(ok=False)), "failed safety checks: mint_authority_revoked"),
    (dict(m=None, note="no pool found (not_found)"), "no pool found"),
    (dict(paused=True), "desk is paused"),
])
def test_buys_that_are_not_copied(change, reason):
    desk = PaperDesk(DeskStore(), now=NOW)
    if change.get("paused"):
        desk.pause("break", NOW)
    step = desk.on_trader_trade(change.get("t", trade()), NOW, change.get("m", market()) if "m" in change or
                                "note" not in change else None, market_note=change.get("note", ""))
    assert step.action == "skipped" and any(reason in r for r in step.reasons), step.reasons
    assert desk.ledgers["copy"].open_symbols() == []


def test_no_second_position_in_the_same_token_and_at_most_five_positions():
    desk, _ = bought_desk()
    again = desk.on_trader_trade(trade(wallet=OTHER, sig="sig2"), NOW, market())
    assert again.action == "skipped" and "already holding this token" in again.reasons
    for i in range(4):
        pool = f"solana:Pool{i}11111111111111111111111111111111111111"[:52]
        step = desk.on_trader_trade(trade(mint=f"Mint{i}", sig=f"s{i}"), NOW + timedelta(minutes=20 * (i + 1)),
                                    market(symbol=pool, at=NOW + timedelta(minutes=20 * (i + 1))))
        assert step.action == "bought", step.reasons
    sixth = desk.on_trader_trade(trade(mint="Mint9", sig="s9"), NOW + timedelta(hours=3),
                                 market(symbol="solana:Pool9", at=NOW + timedelta(hours=3)))
    assert sixth.action == "skipped" and "maximum 5" in sixth.reasons[0]


# --------------------------------------------------------------- copy exits

def test_the_followed_trader_selling_closes_our_position():
    desk, _ = bought_desk()
    later = NOW + timedelta(minutes=7)
    step = desk.on_trader_trade(trade(Side.SELL, sig="sig9", at=later), later, exit_price=D("0.0013"))
    assert step.action == "sold" and step.fills[0].price == D("0.0013") * D("0.99")
    assert desk.ledgers["copy"].quantity(POOL) == 0 and desk.broker.guard(POOL) is None
    trip, = desk.status()["sleeves"]["copy"]["trades"]
    assert trip.exit_reasons == ["trader_exit"] and trip.pnl > 0
    assert desk.copy_symbols() == [] and desk.check() == []


def test_another_traders_sell_is_ignored_and_a_sell_without_a_price_waits_for_the_next_open():
    desk, _ = bought_desk()
    other = desk.on_trader_trade(trade(Side.SELL, wallet=OTHER, sig="x"), NOW, exit_price=D("0.0013"))
    assert other.action == "ignored" and "copied another trader" in other.reasons[0]
    step = desk.on_trader_trade(trade(Side.SELL, sig="y"), NOW + timedelta(seconds=30))
    assert step.action == "selling" and desk.broker.pending_exit(POOL)
    results = feed(desk, minute(1, "0.0012"), minute(2, "0.0011"))   # decided at 21:04:07: fills at 21:05
    assert results[0].fills == [] and results[1].fills[0].price == D("0.0011") * D("0.99")
    assert desk.ledgers["copy"].quantity(POOL) == 0


def test_trailing_stop_on_one_minute_candles_and_bad_copy_data_does_not_degrade_the_desk():
    desk, step = bought_desk()
    entry = step.fills[0].price
    results = feed(desk, minute(0, "0.001", h="0.0005", lo="0.0005"),   # the buy's own minute: ignored
                   minute(1, "0.001", h="0.002", lo="0.001"),          # high 0.002: stop trails to 0.0015
                   minute(2, "0.0019", lo="0.0014", cl="0.0016"), status=DataStatus.DEGRADED)
    assert results[0].fills == [] and desk.state is DeskState.NORMAL
    sell, = results[2].fills
    assert sell.reference_price == D("0.002") * D("0.75") and sell.reference_price > entry
    assert desk.broker.guard(POOL) is None


def test_the_24_hour_limit_sells_a_copy_position_the_trader_never_sold():
    desk, _ = bought_desk()
    feed(desk, minute(1, "0.001"))
    late = minute(60 * 24 + 1, "0.0011")                      # bought at 21:03:37: 24 h passed at 21:04
    result, = feed(desk, late)
    sell, = result.fills
    assert desk.store.orders()[sell.order_id].reason is OrderReason.TIME_LIMIT


# ------------------------------------------------------------------- sleeves

def test_the_sleeves_keep_separate_books_and_survive_a_restart(tmp_path):
    path = tmp_path / "desk.db"
    desk = PaperDesk(DeskStore(path), now=TRADE[0].open_time)
    run(desk, TRADE, stop=65)                                 # the bot holds the test pool
    copy_time = TRADE[64].close_time + timedelta(seconds=30)
    desk.on_trader_trade(trade(at=copy_time), copy_time, market(at=copy_time))
    bot, copy = desk.ledgers["bot"], desk.ledgers["copy"]
    assert bot.open_symbols() == [TRADE[0].symbol] and copy.open_symbols() == [POOL]
    assert bot.initial_cash == copy.initial_cash == 500
    desk.store.close()
    again = PaperDesk(DeskStore(path), now=copy_time)
    assert again.check() == [] and again.state is DeskState.NORMAL
    assert again.broker.guard(POOL).copied_from == FOLLOWED          # still follows the trader's sell
    assert again.ledgers["copy"].cash == copy.cash and again.ledgers["bot"].cash == bot.cash


def test_the_bot_does_not_trade_a_token_the_copy_sleeve_holds():
    desk = PaperDesk(DeskStore(), now=TRADE[0].open_time)
    run(desk, TRADE, stop=63)
    copy_time = TRADE[62].close_time + timedelta(seconds=30)
    desk.on_trader_trade(trade(at=copy_time), copy_time, market(symbol=TRADE[0].symbol, at=copy_time))
    result = run(desk, TRADE, start=63, stop=64)[0]
    assert result.decision is None and "copy sleeve holds" in result.notes[-1]


def test_rebalancing_moves_money_to_the_better_sleeve_and_replays_on_restart(tmp_path):
    config = DeskConfig(rebalance=RebalanceConfig(min_trades=1, every=timedelta(days=1)))
    path = tmp_path / "desk.db"
    desk = PaperDesk(DeskStore(path), config, now=TRADE[0].open_time)
    run(desk, TRADE)                                          # the bot closes one losing trade
    t = TRADE[-1].close_time + timedelta(minutes=1)
    desk.on_trader_trade(trade(at=t), t, market(at=t))
    desk.on_trader_trade(trade(Side.SELL, sig="s2", at=t + timedelta(minutes=5)), t + timedelta(minutes=5),
                         exit_price=D("0.0015"))             # the copy sleeve closes one winner
    later = TRADE[-1].close_time + timedelta(days=1)
    nxt = make_candle(later, symbol=TRADE[0].symbol, open=TRADE[-1].close, high=TRADE[-1].close,
                      low=TRADE[-1].close, close=TRADE[-1].close, volume=D(1_000_000))
    desk.on_candle(CandleInputs(nxt, report(), watched=True), now=nxt.close_time)
    move, = desk.store.records("transfer")
    assert move["from"] == "bot" and move["to"] == "copy"
    total = sum(desk.ledgers[s].cash for s in ("bot", "copy"))
    assert abs(D(move["amount"]) - total * D("0.1")) <= D("1e-8")   # 10% of total equity (nothing was held)
    assert desk.ledgers["bot"].initial_cash == 500 - D(move["amount"])
    desk.store.close()
    again = PaperDesk(DeskStore(path), config, now=later)
    assert again.check() == [] and again.ledgers["copy"].initial_cash == 500 + D(move["amount"])


def test_weights_must_be_valid():
    with pytest.raises(ValueError):
        DeskConfig(weights=(("bot", D("0.9")), ("copy", D("0.1"))))   # below the 20% floor
    with pytest.raises(ValueError):
        DeskConfig(weights=(("bot", D("0.5")), ("copy", D("0.4"))))   # doesn't add up to 1
