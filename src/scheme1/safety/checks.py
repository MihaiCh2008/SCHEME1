"""Token-safety checks. Every check must PASS for a token to be tradable; UNVERIFIED blocks like FAIL.

Conservative rules when the providers disagree:
- liquidity: the lower figure is used, and if the two differ by more than ``max_liquidity_disagreement``
  the check is UNVERIFIED (one of them is probably wrong or stale);
- pool age: the youngest creation date reported is used (they disagreed by six months for BONK/SOL);
- base token: both providers must name the same mint.

Mint authority, freeze authority and holder concentration need Solana on-chain data. No source is
connected yet (the public RPC is blocked from the cloud session), so they are UNVERIFIED and every token
is currently blocked. That is intended: see DECISIONS.md D9.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from scheme1.contracts import PoolSnapshot, SafetyCheck, SafetyReport, SafetyStatus

SAFETY_CONFIG_VERSION = "s1.0.0"


@dataclass(frozen=True)
class SafetyConfig:
    min_liquidity_usd: Decimal = Decimal(25_000)
    max_liquidity_disagreement: Decimal = Decimal("1.5")  # higher / lower
    min_pool_age: timedelta = timedelta(hours=24)
    max_snapshot_age: timedelta = timedelta(minutes=10)
    version: str = SAFETY_CONFIG_VERSION


ONCHAIN_CHECKS = ("mint_authority_revoked", "freeze_authority_revoked", "holder_concentration")


def evaluate(symbol: str, snapshots: list[PoolSnapshot], now: datetime,
             config: SafetyConfig = SafetyConfig()) -> SafetyReport:
    """``snapshots`` holds whatever the providers returned (a failed provider is simply missing)."""
    fresh = [s for s in snapshots if s.symbol == symbol and now - s.retrieved_at <= config.max_snapshot_age]
    stale = sorted({s.provider for s in snapshots} - {s.provider for s in fresh})
    note = f"ignored stale or foreign snapshots from {', '.join(stale)}" if stale else ""
    checks = [
        _base_token(fresh, note),
        _liquidity(fresh, config, note),
        _pool_age(fresh, now, config, note),
        *(SafetyCheck(name=name, status=SafetyStatus.UNVERIFIED, observed="no data", limit="required",
                      detail="needs Solana on-chain data; no source connected yet") for name in ONCHAIN_CHECKS),
    ]
    return SafetyReport(symbol=symbol, checked_at=now, checks=tuple(checks), safety_config_version=config.version,
                        tradable=all(c.status is SafetyStatus.PASS for c in checks))


def _base_token(snaps: list[PoolSnapshot], note: str) -> SafetyCheck:
    mints = {s.provider: s.base_token_mint for s in snaps if s.base_token_mint}
    observed = "; ".join(f"{p}={m}" for p, m in sorted(mints.items())) or "no data"
    if len(mints) < 2:
        return SafetyCheck(name="base_token_agrees", status=SafetyStatus.UNVERIFIED, observed=observed,
                           limit="two providers, same mint", detail=note or "fewer than two providers reported it")
    ok = len(set(mints.values())) == 1
    return SafetyCheck(name="base_token_agrees", status=SafetyStatus.PASS if ok else SafetyStatus.UNVERIFIED,
                       observed=observed, limit="two providers, same mint",
                       detail="" if ok else "providers name different tokens for this pool")


def _liquidity(snaps: list[PoolSnapshot], config: SafetyConfig, note: str) -> SafetyCheck:
    values = {s.provider: s.liquidity_usd for s in snaps if s.liquidity_usd is not None}
    observed = "; ".join(f"{p}=${v:,.0f}" for p, v in sorted(values.items())) or "no data"
    limit = f">= ${config.min_liquidity_usd:,.0f} (lower of two providers)"
    if len(values) < 2:
        return SafetyCheck(name="min_liquidity", status=SafetyStatus.UNVERIFIED, observed=observed, limit=limit,
                           detail=note or "fewer than two providers reported liquidity")
    low, high = min(values.values()), max(values.values())
    if low == 0 or high / low > config.max_liquidity_disagreement:
        return SafetyCheck(name="min_liquidity", status=SafetyStatus.UNVERIFIED, observed=observed, limit=limit,
                           detail=f"providers disagree by more than {config.max_liquidity_disagreement}x")
    ok = low >= config.min_liquidity_usd
    return SafetyCheck(name="min_liquidity", status=SafetyStatus.PASS if ok else SafetyStatus.FAIL,
                       observed=observed, limit=limit)


def _pool_age(snaps: list[PoolSnapshot], now: datetime, config: SafetyConfig, note: str) -> SafetyCheck:
    dates = {s.provider: s.pool_created_at for s in snaps if s.pool_created_at is not None}
    observed = "; ".join(f"{p}={d.isoformat()}" for p, d in sorted(dates.items())) or "no data"
    limit = f">= {config.min_pool_age} old (youngest date reported)"
    if not dates:
        return SafetyCheck(name="min_pool_age", status=SafetyStatus.UNVERIFIED, observed=observed, limit=limit,
                           detail=note or "no provider reported a creation date")
    youngest = max(dates.values())
    if youngest > now:
        return SafetyCheck(name="min_pool_age", status=SafetyStatus.UNVERIFIED, observed=observed, limit=limit,
                           detail="creation date is in the future")
    ok = now - youngest >= config.min_pool_age
    return SafetyCheck(name="min_pool_age", status=SafetyStatus.PASS if ok else SafetyStatus.FAIL,
                       observed=observed, limit=limit, detail=f"age {now - youngest}")
