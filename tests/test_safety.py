import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from scheme1.contracts import PoolSnapshot, SafetyCheck, SafetyReport, SafetyStatus
from scheme1.data.http import HttpResponse, PacedClient
from scheme1.data.providers.base import ProviderError
from scheme1.safety.checks import ONCHAIN_CHECKS, SafetyConfig, evaluate
from scheme1.safety.pools import DexScreenerPools, GeckoTerminalPools
from data_helpers import FakeClock, ScriptedTransport

POOL = "Poo11111111111111111111111111111111111111111"[:44]  # base58: no 0, O, I or l
SYM = f"solana:{POOL}"
MINT = "Mint1111111111111111111111111111111111111111"[:44]
NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def client(*bodies):
    return PacedClient(ScriptedTransport(*[HttpResponse(200, b) for b in bodies]), min_interval_s=0, clock=FakeClock())


# Synthetic values in the layouts both APIs returned on 2026-10-12.
def gt_body(address=POOL, reserve="357858.9424", created="2023-07-05T14:41:09Z", mint=MINT):
    return json.dumps({"data": {"id": f"solana_{address}", "type": "pool",
                                "attributes": {"address": address, "reserve_in_usd": reserve, "pool_created_at": created},
                                "relationships": {"base_token": {"data": {"id": f"solana_{mint}", "type": "token"}},
                                                  "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112"}}}}}).encode()


def ds_body(address=POOL, liquidity=358305.84, created_ms=1671980424000, mint=MINT):
    return json.dumps({"schemaVersion": "1.0.0", "pairs": [{
        "chainId": "solana", "pairAddress": address, "baseToken": {"address": mint, "symbol": "SYN"},
        "liquidity": {"usd": liquidity, "base": 1, "quote": 1}, "pairCreatedAt": created_ms}]}).encode()


def test_parses_geckoterminal_pool():
    snap = GeckoTerminalPools(client(gt_body()), now=lambda: NOW).snapshot(SYM)
    assert snap.liquidity_usd == Decimal("357858.9424") and snap.base_token_mint == MINT
    assert snap.pool_created_at == datetime(2023, 7, 5, 14, 41, 9, tzinfo=timezone.utc)


def test_parses_dexscreener_pair():
    snap = DexScreenerPools(client(ds_body()), now=lambda: NOW).snapshot(SYM)
    assert snap.liquidity_usd == Decimal("358305.84") and snap.base_token_mint == MINT
    assert snap.pool_created_at == datetime(2022, 12, 25, 15, 0, 24, tzinfo=timezone.utc)


@pytest.mark.parametrize("make,body,reason", [
    (GeckoTerminalPools, gt_body(address="Other111111111111111111111111111111111111111"), "wrong_pool"),
    (GeckoTerminalPools, b'{"data": {}}', "bad_payload"),
    (GeckoTerminalPools, b"<html>", "bad_payload"),
    (DexScreenerPools, b'{"schemaVersion":"1.0.0","pairs":null}', "not_found"),
    (DexScreenerPools, ds_body(address="Other111111111111111111111111111111111111111"), "wrong_pool"),
])
def test_provider_problems_raise(make, body, reason):
    with pytest.raises(ProviderError) as err:
        make(client(body), now=lambda: NOW).snapshot(SYM)
    assert err.value.reason == reason


def test_missing_fields_become_none():
    body = json.dumps({"data": {"attributes": {"address": POOL, "reserve_in_usd": None}}}).encode()
    snap = GeckoTerminalPools(client(body), now=lambda: NOW).snapshot(SYM)
    assert snap.liquidity_usd is None and snap.pool_created_at is None and snap.base_token_mint is None


def snap(provider, liquidity="100000", created=NOW - timedelta(days=30), mint=MINT, at=NOW):
    return PoolSnapshot(symbol=SYM, provider=provider, retrieved_at=at,
                        liquidity_usd=Decimal(liquidity) if liquidity is not None else None,
                        pool_created_at=created, base_token_mint=mint, content_sha256="0" * 64)


def statuses(report):
    return {c.name: c.status for c in report.checks}


def test_good_pool_still_blocked_by_unverified_onchain_checks():
    report = evaluate(SYM, [snap("geckoterminal"), snap("dexscreener", "110000")], NOW)
    st = statuses(report)
    assert st["base_token_agrees"] == st["min_liquidity"] == st["min_pool_age"] == SafetyStatus.PASS
    assert all(st[name] is SafetyStatus.UNVERIFIED for name in ONCHAIN_CHECKS)
    assert report.tradable is False


def test_liquidity_uses_lower_figure_and_flags_disagreement():
    st = statuses(evaluate(SYM, [snap("geckoterminal", "30000"), snap("dexscreener", "20000")], NOW))
    assert st["min_liquidity"] is SafetyStatus.FAIL  # 20k < 25k
    st = statuses(evaluate(SYM, [snap("geckoterminal", "100000"), snap("dexscreener", "50000")], NOW))
    assert st["min_liquidity"] is SafetyStatus.UNVERIFIED  # 2x apart
    st = statuses(evaluate(SYM, [snap("geckoterminal", "100000")], NOW))
    assert st["min_liquidity"] is SafetyStatus.UNVERIFIED  # one source only


def test_pool_age_uses_youngest_date():
    old, young = NOW - timedelta(days=300), NOW - timedelta(hours=2)
    st = statuses(evaluate(SYM, [snap("geckoterminal", created=young), snap("dexscreener", created=old)], NOW))
    assert st["min_pool_age"] is SafetyStatus.FAIL
    st = statuses(evaluate(SYM, [snap("geckoterminal", created=NOW + timedelta(hours=1)),
                                 snap("dexscreener")], NOW))
    assert st["min_pool_age"] is SafetyStatus.UNVERIFIED


def test_mints_must_agree():
    other = "Other111111111111111111111111111111111111111"
    st = statuses(evaluate(SYM, [snap("geckoterminal"), snap("dexscreener", mint=other)], NOW))
    assert st["base_token_agrees"] is SafetyStatus.UNVERIFIED


def test_stale_snapshots_are_ignored():
    stale = NOW - SafetyConfig().max_snapshot_age - timedelta(seconds=1)
    report = evaluate(SYM, [snap("geckoterminal", at=stale), snap("dexscreener")], NOW)
    check = next(c for c in report.checks if c.name == "min_liquidity")
    assert check.status is SafetyStatus.UNVERIFIED and "geckoterminal" in check.detail


def test_no_data_at_all_blocks_everything():
    report = evaluate(SYM, [], NOW)
    assert report.tradable is False and all(c.status is SafetyStatus.UNVERIFIED for c in report.checks)


def test_report_contract_ties_tradable_to_checks():
    passed = SafetyCheck(name="x", status=SafetyStatus.PASS, observed="1", limit="1")
    failed = passed.model_copy(update={"status": SafetyStatus.UNVERIFIED})
    SafetyReport(symbol=SYM, checked_at=NOW, checks=(passed,), safety_config_version="s", tradable=True)
    with pytest.raises(ValidationError):
        SafetyReport(symbol=SYM, checked_at=NOW, checks=(passed, failed), safety_config_version="s", tradable=True)
    with pytest.raises(ValidationError):
        SafetyReport(symbol=SYM, checked_at=NOW, checks=(), safety_config_version="s", tradable=True)
