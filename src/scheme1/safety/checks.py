"""Token-safety checks. Every check must PASS for a token to be tradable; UNVERIFIED blocks like FAIL.

Conservative rules when the providers disagree:
- liquidity: the lower figure is used, and if the two differ by more than ``max_liquidity_disagreement``
  the check is UNVERIFIED (one of them is probably wrong or stale);
- pool age: the youngest creation date reported is used (they disagreed by six months for BONK/SOL);
- base token: both providers must name the same mint.

On-chain checks (from ``TokenOnchainInfo``, read via Solana RPC):
- the mint and freeze authorities must be revoked (null);
- Token-2022 extensions must all be on an allow-list of harmless metadata extensions; known
  dangerous ones (permanent delegate, transfer hook, transfer fee, frozen-by-default, ...) FAIL,
  and unknown ones are UNVERIFIED;
- holder concentration counts only wallets (on-curve owners). Program-derived owners such as
  pool vaults, bonding curves and lockers are reported but excluded. If fewer than 10 wallets
  appear in the largest accounts, each missing one is assumed as large as the smallest account
  seen, so the figure is an upper bound.
Without on-chain data these checks are UNVERIFIED, so the token is blocked (DECISIONS.md D9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from scheme1.contracts import PoolSnapshot, SafetyCheck, SafetyReport, SafetyStatus, TokenOnchainInfo

SAFETY_CONFIG_VERSION = "s1.1.0"


@dataclass(frozen=True)
class SafetyConfig:
    min_liquidity_usd: Decimal = Decimal(25_000)
    max_liquidity_disagreement: Decimal = Decimal("1.5")  # higher / lower
    min_pool_age: timedelta = timedelta(hours=24)
    max_snapshot_age: timedelta = timedelta(minutes=10)
    max_top_holder_pct: Decimal = Decimal(10)  # largest single wallet, % of supply
    max_top10_holders_pct: Decimal = Decimal(30)  # ten largest wallets together, % of supply
    version: str = SAFETY_CONFIG_VERSION


ONCHAIN_CHECKS = ("mint_authority_revoked", "freeze_authority_revoked", "token_extensions_safe",
                  "holder_concentration")
SAFE_EXTENSIONS = frozenset({"metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer",
                             "tokenGroup", "tokenGroupMember"})
# Each of these lets the creator take, block or tax holders' tokens, or hide balances.
DANGEROUS_EXTENSIONS = frozenset({"permanentDelegate", "transferHook", "transferFeeConfig", "defaultAccountState",
                                  "nonTransferable", "pausableConfig", "confidentialTransferMint"})


def evaluate(symbol: str, snapshots: list[PoolSnapshot], now: datetime,
             config: SafetyConfig = SafetyConfig(), onchain: TokenOnchainInfo | None = None) -> SafetyReport:
    """``snapshots`` and ``onchain`` hold whatever the providers returned (a failed provider is simply missing)."""
    fresh = [s for s in snapshots if s.symbol == symbol and now - s.retrieved_at <= config.max_snapshot_age]
    stale = sorted({s.provider for s in snapshots} - {s.provider for s in fresh})
    note = f"ignored stale or foreign snapshots from {', '.join(stale)}" if stale else ""
    checks = [
        _base_token(fresh, note),
        _liquidity(fresh, config, note),
        _pool_age(fresh, now, config, note),
        *_onchain_checks(fresh, onchain, now, config),
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


def _onchain_checks(snaps: list[PoolSnapshot], info: TokenOnchainInfo | None, now: datetime,
                    config: SafetyConfig) -> list[SafetyCheck]:
    pool_mints = {s.base_token_mint for s in snaps if s.base_token_mint}
    problem = None
    if info is None:
        problem = "no on-chain data (Solana RPC unavailable)"
    elif now - info.retrieved_at > config.max_snapshot_age:
        problem = "on-chain data is stale"
    elif pool_mints != {info.mint}:
        problem = f"on-chain data is for {info.mint}, pool reports {sorted(pool_mints) or 'no mint'}"
    elif info.supply == 0:
        problem = "token supply is zero"
    if problem:
        return [SafetyCheck(name=n, status=SafetyStatus.UNVERIFIED, observed="no data", limit="required",
                            detail=problem) for n in ONCHAIN_CHECKS]
    assert info is not None
    return [
        _authority("mint_authority_revoked", info.mint_authority, "creator could mint unlimited new supply"),
        _authority("freeze_authority_revoked", info.freeze_authority, "creator could freeze holders' tokens"),
        _extensions(info),
        _concentration(info, config),
    ]


def _authority(name: str, authority: str | None, risk: str) -> SafetyCheck:
    if authority is None:
        return SafetyCheck(name=name, status=SafetyStatus.PASS, observed="revoked", limit="revoked")
    return SafetyCheck(name=name, status=SafetyStatus.FAIL, observed=authority, limit="revoked", detail=risk)


def _extensions(info: TokenOnchainInfo) -> SafetyCheck:
    observed = f"{info.token_program}: {', '.join(info.extensions) or 'none'}"
    limit = "only metadata extensions"
    dangerous = sorted(set(info.extensions) & DANGEROUS_EXTENSIONS)
    unknown = sorted(set(info.extensions) - DANGEROUS_EXTENSIONS - SAFE_EXTENSIONS)
    if dangerous:
        return SafetyCheck(name="token_extensions_safe", status=SafetyStatus.FAIL, observed=observed, limit=limit,
                           detail=f"dangerous: {', '.join(dangerous)}")
    if unknown:
        return SafetyCheck(name="token_extensions_safe", status=SafetyStatus.UNVERIFIED, observed=observed,
                           limit=limit, detail=f"not reviewed: {', '.join(unknown)}")
    return SafetyCheck(name="token_extensions_safe", status=SafetyStatus.PASS, observed=observed, limit=limit)


def _pct(amount: int, supply: int) -> Decimal:
    return Decimal(amount) * 100 / Decimal(supply)


def _concentration(info: TokenOnchainInfo, config: SafetyConfig) -> SafetyCheck:
    wallets = [h for h in info.holders if not h.program_controlled]
    programs = [h for h in info.holders if h.program_controlled]
    top10 = sum(h.amount for h in wallets[:10])
    missing = max(0, 10 - len(wallets))
    if missing and info.accounts_seen >= 20:  # list was cut off: unseen wallets are at most the smallest seen
        top10 += missing * info.smallest_account_seen
    top1 = wallets[0].amount if wallets else 0
    top1_pct, top10_pct = _pct(top1, info.supply), _pct(top10, info.supply)
    observed = (f"largest wallet {top1_pct:.2f}%, top 10 wallets {top10_pct:.2f}%; "
                f"program-controlled (excluded) {_pct(sum(h.amount for h in programs), info.supply):.2f}%")
    limit = f"largest <= {config.max_top_holder_pct}%, top 10 <= {config.max_top10_holders_pct}%"
    ok = top1_pct <= config.max_top_holder_pct and top10_pct <= config.max_top10_holders_pct
    return SafetyCheck(name="holder_concentration", status=SafetyStatus.PASS if ok else SafetyStatus.FAIL,
                       observed=observed, limit=limit,
                       detail="" if ok else "a few wallets could dump on the pool")
