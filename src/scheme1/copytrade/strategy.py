"""Copy-trading settings and market lookups (D22, D23). Simulation only.

A followed trader buys a token → the desk buys a small amount right away, if the token passes the copy
safety checks. The position is closed when that trader sells, on a trailing stop, or after 24 hours.

Every number here is a first guess, to be tested in Stage 8 (backtesting).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from scheme1.broker import BrokerConfig, ExitPlan
from scheme1.contracts import SafetyReport
from scheme1.data.providers.base import ProviderError
from scheme1.risk import RiskConfig
from scheme1.safety.checks import COPY_SAFETY, SafetyConfig, evaluate
from scheme1.safety.pools import DexScreenerPools, GeckoTerminalPools
from scheme1.safety.solana import SolanaRpc

COPY_STRATEGY_VERSION = "copy-1.0.0"


@dataclass(frozen=True)
class CopyConfig:
    position_pct: Decimal = Decimal(5)  # of the copy sleeve's equity, per trade
    max_positions: int = 5  # also keeps the 1-minute candle requests inside GeckoTerminal's free limit
    min_buy: tuple[tuple[str, Decimal], ...] = (("SOL", Decimal("0.1")), ("USDC", Decimal(10)), ("USDT", Decimal(10)))
    follow_sells: bool = True  # sell when the trader we copied sells any of the token
    cost_buffer_pct: Decimal = Decimal(3)  # kept aside for the fee and slippage when sizing
    # pump.fun coins swing hard: a wide stop that trails 25% under the highest price, no take profit
    # (the trader's sell is the planned exit), and a 24-hour limit in case the trader never sells.
    exits: ExitPlan = field(default_factory=lambda: ExitPlan(stop_loss_pct=Decimal(20), take_profit_pct=None,
                                                            trail_pct=Decimal(25), max_hold=timedelta(hours=24)))
    # pump.fun's fee is about 1% a trade; thin pools move more on each fill than big ones
    broker: BrokerConfig = field(default_factory=lambda: BrokerConfig(fee_pct=Decimal(1), slippage_pct=Decimal(1)))
    risk: RiskConfig = field(default_factory=lambda: RiskConfig(fee_pct=Decimal(1), slippage_buffer_pct=Decimal(2),
                                                                max_data_age=timedelta(minutes=2)))
    safety: SafetyConfig = COPY_SAFETY
    version: str = COPY_STRATEGY_VERSION

    def __post_init__(self) -> None:
        if not 0 < self.position_pct <= self.risk.max_position_pct:
            raise ValueError("a copy trade must fit under the risk engine's position limit")
        if self.risk.fee_pct < self.broker.fee_pct or self.risk.slippage_buffer_pct < self.broker.slippage_pct:
            raise ValueError("the copy risk check assumes lower costs than the copy broker charges")

    def min_buy_for(self, quote: str) -> Decimal:
        return dict(self.min_buy).get(quote, Decimal("Infinity"))


@dataclass(frozen=True)
class CopyMarket:
    """What the desk needs to copy a buy: the token's pool, its safety report and its current price."""

    symbol: str
    safety: SafetyReport
    price_usd: Decimal  # the higher of the providers' prices (conservative for a buy)
    priced_at: datetime


def resolve_buy(mint: str, gecko: GeckoTerminalPools, dex: DexScreenerPools, rpc: SolanaRpc,
                safety: SafetyConfig, now: datetime) -> tuple[CopyMarket | None, str]:
    """Find the token's main pool, check it and price it. Returns (market, "") or (None, why not)."""
    try:
        main = gecko.main_pool(mint)
    except ProviderError as err:
        return None, f"no pool found ({err.reason})"
    snaps = [main]
    try:
        snaps.append(dex.snapshot(main.symbol))
    except ProviderError:
        pass  # the safety report says which checks lacked a second source
    try:
        onchain = rpc.token_info(mint)
    except (ProviderError, ValueError):
        onchain = None
    report = evaluate(main.symbol, snaps, now, safety, onchain=onchain)
    prices = [s.base_price_usd for s in snaps if s.base_price_usd]
    if not prices:
        return None, "no price for the token"
    return CopyMarket(symbol=main.symbol, safety=report, price_usd=max(prices), priced_at=now), ""


def exit_price(symbol: str, dex: DexScreenerPools, gecko: GeckoTerminalPools) -> Decimal | None:
    """The lower current price (conservative for a sell). DexScreener first: it doesn't use GeckoTerminal's
    small request budget."""
    for provider in (dex, gecko):
        try:
            price = provider.snapshot(symbol).base_price_usd
        except ProviderError:
            continue
        if price:
            return price
    return None
