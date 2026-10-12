import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scheme1.contracts import HolderBalance, PoolSnapshot, SafetyStatus, TokenOnchainInfo
from scheme1.data.http import HttpResponse, PacedClient, RetryPolicy
from scheme1.data.providers.base import ProviderError
from scheme1.safety.checks import ONCHAIN_CHECKS, evaluate
from scheme1.safety.solana import SolanaRpc, b58decode, is_on_curve
from data_helpers import FakeClock, ScriptedTransport

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # real mint address; all amounts below are synthetic
PDA = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"   # Raydium AMM v4 authority: a program-derived address
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PS1H7nPZo8q1bD"

# Ed25519 public keys generated on 2026-10-12 with the `cryptography` package, so they are on the curve.
WALLETS = [
    "m7gv7zmexAbfPJzK2rapUGGMpk8bPKDV2C5tjd1KaTb",
    "FhW1Wx49gfLj4ydNRqkKVoDrYyc6v2cjRSgQBMeXgQyU",
    "5Pr9tkSCHMWeowJue8iDemcMF7S2naEJek3t1FXbhJcZ",
    "4WJjiBFheeuN68Y931hGoQAHQ7cMYRzseeMbs6grkr8H",
    "DcWMndfwjoYUS2YZMXbCJxFoECTURxiVwLzr7vbnDmc3",
    "BNfnnGaMBs2px2inH8ASYmHEYBF62b6btagLtmWLedmZ",
    "EHisuy6nJEXi7V1KiJhSQDPwxE2ZKf2g8CmyVX32uM12",
    "BZDG9G7kvKWycazNUnLCAbq9yNVjreji6hx5sbnV6C5j",
    "4Zg39toop9QfvyZvuLK4SvTC2gcCJeQzAmxNXGvPFVZr",
    "6xsruavDAC3ge4GkAK4Gnmw1AA1c2aiDZSj93JpaMR2L",
    "CWMxDASMWCGhSswB58E9RScad1eFWJ1ozfm7agXaz1RK",
    "FWcKxWfecXeAtiSEBxbk5PLpWiwdLR4L8WgLJL8tt2Jc"
]
# Random 32-byte values that failed the curve test (stand-ins for program-derived addresses).
OFF_CURVE = [
    "Edxm6kdNyk1bpX8VYnokaXZfn8sQHa78yybaZybMtBQy",
    "2DeKQY7byLkPZwXEZobR5oEuZLofchhwkMkQjijeDDfN",
    "nt24MzgwQ4krf4dQGtTaDfNeGuziMcfKk4Mo7JGZGr4"
]


# ---------------------------------------------------------------- curve test

def test_wallet_keys_are_on_curve_and_pdas_are_not():
    assert all(is_on_curve(w) for w in WALLETS)
    assert not any(is_on_curve(a) for a in OFF_CURVE)
    assert not is_on_curve(PDA)
    assert is_on_curve("11111111111111111111111111111111")  # the all-zero key decodes to a valid point


def test_base58():
    assert b58decode("11111111111111111111111111111111") == bytes(32)
    assert len(b58decode(MINT)) == 32
    with pytest.raises(ValueError):
        b58decode("0OIl")
    with pytest.raises(ValueError):
        is_on_curve("1111")


# ----------------------------------------------------------------------- RPC

def rpc_ok(result):
    return HttpResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode())


def mint_account(mint_authority=None, freeze_authority=None, program=TOKEN_PROGRAM, extensions=None,
                 supply="1000000000"):
    info = {"decimals": 6, "freezeAuthority": freeze_authority, "isInitialized": True,
            "mintAuthority": mint_authority, "supply": supply}
    if extensions is not None:
        info["extensions"] = [{"extension": e, "state": {}} for e in extensions]
    name = "spl-token" if program == TOKEN_PROGRAM else "spl-token-2022"
    return rpc_ok({"context": {"slot": 1}, "value": {
        "data": {"parsed": {"info": info, "type": "mint"}, "program": name, "space": 82},
        "executable": False, "lamports": 1, "owner": program}})


def largest(amounts):
    return rpc_ok({"context": {"slot": 1}, "value": [
        {"address": f"acct{i}", "amount": str(a), "decimals": 6} for i, a in enumerate(amounts)]})


def owners(owner_list, mint=MINT):
    return rpc_ok({"context": {"slot": 1}, "value": [
        {"data": {"parsed": {"info": {"mint": mint, "owner": o, "state": "initialized"}, "type": "account"},
                  "program": "spl-token", "space": 165}} for o in owner_list]})


def rpc(*responses, url="https://rpc.example/?api-key=SECRET"):
    transport = ScriptedTransport(*responses)
    client = PacedClient(transport, min_interval_s=0, retry=RetryPolicy(max_attempts=2), clock=FakeClock())
    return SolanaRpc(client, url=url, now=lambda: NOW), transport


def test_token_info_reads_mint_and_groups_holders_by_owner():
    s, transport = rpc(mint_account(), largest([500, 300, 100]), owners([PDA, WALLETS[0], WALLETS[0]]))
    info = s.token_info(MINT)
    assert info.supply == 1_000_000_000 and info.decimals == 6
    assert info.mint_authority is None and info.freeze_authority is None and info.token_program == "spl-token"
    assert info.holders == (HolderBalance(owner=PDA, amount=500, program_controlled=True),
                            HolderBalance(owner=WALLETS[0], amount=400, program_controlled=False))
    assert info.accounts_seen == 3 and info.smallest_account_seen == 100
    methods = [json.loads(b)["method"] for b in transport.bodies]
    assert methods == ["getAccountInfo", "getTokenLargestAccounts", "getMultipleAccounts"]


def test_token_2022_extensions_are_reported():
    s, _ = rpc(mint_account(program=TOKEN_2022, extensions=["metadataPointer", "transferHook"]),
               largest([]))
    info = s.token_info(MINT)
    assert info.token_program == "spl-token-2022" and info.extensions == ("metadataPointer", "transferHook")


@pytest.mark.parametrize("responses,reason", [
    ([HttpResponse(200, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid param"}}')], "rpc_error"),
    ([rpc_ok({"context": {}, "value": None})], "not_found"),
    ([mint_account(program="11111111111111111111111111111111")], "not_a_mint"),
    ([mint_account(), largest([5]), owners([WALLETS[0]], mint="So11111111111111111111111111111111111111112")],
     "bad_payload"),
    ([HttpResponse(200, b"<html>")], "bad_payload"),
    ([HttpResponse(429, b"slow down"), HttpResponse(429, b"slow down")], "rate_limited"),
])
def test_rpc_problems_raise_without_leaking_the_url(responses, reason):
    s, _ = rpc(*responses)
    with pytest.raises(ProviderError) as err:
        s.token_info(MINT)
    assert err.value.reason == reason
    assert "SECRET" not in str(err.value)


def test_rpc_url_from_environment(monkeypatch):
    monkeypatch.setenv("SCHEME1_SOLANA_RPC_URL", "https://custom.example/rpc")
    transport = ScriptedTransport(mint_account(), largest([]))
    SolanaRpc(PacedClient(transport, min_interval_s=0, clock=FakeClock()), now=lambda: NOW).token_info(MINT)
    assert transport.urls[0] == "https://custom.example/rpc"


# -------------------------------------------------------------------- checks

POOL_SYM = "solana:5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"


def pool_snaps(mint=MINT):
    return [PoolSnapshot(symbol=POOL_SYM, provider=p, retrieved_at=NOW, liquidity_usd=Decimal("100000"),
                         pool_created_at=NOW - timedelta(days=30), base_token_mint=mint, content_sha256="0" * 64)
            for p in ("geckoterminal", "dexscreener")]


def token(holders=None, supply=1_000_000, accounts_seen=None, smallest=None, **kw):
    holders = holders if holders is not None else [(PDA, 400_000)] + [(w, 10_000) for w in WALLETS[:10]]
    hb = tuple(HolderBalance(owner=o, amount=a, program_controlled=not is_on_curve(o)) for o, a in holders)
    base = dict(mint=MINT, retrieved_at=NOW, token_program="spl-token", supply=supply, decimals=6,
                mint_authority=None, freeze_authority=None, holders=hb,
                accounts_seen=accounts_seen if accounts_seen is not None else len(hb),
                smallest_account_seen=smallest if smallest is not None else min((a for _, a in holders), default=0))
    base.update(kw)
    return TokenOnchainInfo(**base)


def check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_safe_token_is_tradable():
    report = evaluate(POOL_SYM, pool_snaps(), NOW, onchain=token())
    assert report.tradable, [(c.name, c.status, c.detail) for c in report.checks]
    # 40% sits in the pool vault (program-controlled) and is excluded; wallets hold 1% each
    assert "program-controlled (excluded) 40.00%" in check(report, "holder_concentration").observed


@pytest.mark.parametrize("kw,name", [
    ({"mint_authority": WALLETS[11]}, "mint_authority_revoked"),
    ({"freeze_authority": WALLETS[11]}, "freeze_authority_revoked"),
    ({"token_program": "spl-token-2022", "extensions": ("metadataPointer", "permanentDelegate")},
     "token_extensions_safe"),
])
def test_dangerous_mint_settings_fail(kw, name):
    report = evaluate(POOL_SYM, pool_snaps(), NOW, onchain=token(**kw))
    assert check(report, name).status is SafetyStatus.FAIL and not report.tradable


def test_harmless_and_unreviewed_extensions():
    ok = token(token_program="spl-token-2022", extensions=("metadataPointer", "tokenMetadata"))
    assert check(evaluate(POOL_SYM, pool_snaps(), NOW, onchain=ok), "token_extensions_safe").status is SafetyStatus.PASS
    odd = token(token_program="spl-token-2022", extensions=("someNewExtension",))
    assert check(evaluate(POOL_SYM, pool_snaps(), NOW, onchain=odd), "token_extensions_safe").status \
        is SafetyStatus.UNVERIFIED


def test_whale_wallet_fails_but_pool_vault_does_not():
    whale = token(holders=[(WALLETS[0], 150_000)] + [(w, 1_000) for w in WALLETS[1:10]])
    assert check(evaluate(POOL_SYM, pool_snaps(), NOW, onchain=whale), "holder_concentration").status \
        is SafetyStatus.FAIL
    vault = token(holders=[(PDA, 900_000)] + [(w, 1_000) for w in WALLETS[:10]])
    assert check(evaluate(POOL_SYM, pool_snaps(), NOW, onchain=vault), "holder_concentration").status \
        is SafetyStatus.PASS


def test_top10_is_an_upper_bound_when_the_list_is_cut_off():
    # 20 accounts seen, only 2 wallets among them: 8 unseen wallets are assumed as big as the smallest seen (20k)
    holders = [(a, 40_000) for a in OFF_CURVE] + [(WALLETS[0], 30_000), (WALLETS[1], 30_000)]
    info = token(holders=holders, accounts_seen=20, smallest=20_000)
    c = check(evaluate(POOL_SYM, pool_snaps(), NOW, onchain=info), "holder_concentration")
    assert "top 10 wallets 22.00%" in c.observed and c.status is SafetyStatus.PASS
    info = token(holders=holders, accounts_seen=20, smallest=40_000)
    assert check(evaluate(POOL_SYM, pool_snaps(), NOW, onchain=info), "holder_concentration").status \
        is SafetyStatus.FAIL


@pytest.mark.parametrize("onchain,snaps", [
    (None, pool_snaps()),                                             # RPC unavailable
    (token(retrieved_at=NOW - timedelta(hours=1)), pool_snaps()),     # stale
    (token(), pool_snaps(mint=WALLETS[5])),                           # data is for a different token
    (token(supply=0, holders=[]), pool_snaps()),                      # zero supply
])
def test_unusable_onchain_data_leaves_checks_unverified(onchain, snaps):
    report = evaluate(POOL_SYM, snaps, NOW, onchain=onchain)
    assert all(check(report, n).status is SafetyStatus.UNVERIFIED for n in ONCHAIN_CHECKS)
    assert not report.tradable
