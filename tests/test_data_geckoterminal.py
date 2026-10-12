import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scheme1.contracts import DataKind, DataStatus, Interval, IssueCode
from scheme1.data.http import HttpResponse, PacedClient, RetryPolicy
from scheme1.data.providers import ProviderError
from scheme1.data.providers.geckoterminal import GeckoTerminalProvider, split_symbol
from scheme1.data.validate import validate_batch
from data_helpers import FakeClock, ScriptedTransport

RECORDED = Path(__file__).parent / "fixtures" / "geckoterminal_bonk_sol_5m_recorded.json"
BONK = "solana:5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"
RECORDED_AT = datetime(2026, 10, 12, 18, 21, 22, tzinfo=timezone.utc)


def provider(*script, now=RECORDED_AT):
    transport = ScriptedTransport(*script)
    client = PacedClient(transport, min_interval_s=0, retry=RetryPolicy(max_attempts=2), clock=FakeClock())
    return GeckoTerminalProvider(client, now=lambda: now), transport


def body(rows):
    return json.dumps({"data": {"attributes": {"ohlcv_list": rows}}}).encode()


def test_parses_recorded_response_exactly():
    raw = RECORDED.read_bytes()
    gt, transport = provider(HttpResponse(200, raw))
    result = gt.fetch_candles(BONK, Interval.M5, limit=5)
    batch = result.batch

    assert result.raw == raw
    assert batch.provenance.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert batch.provenance.kind is DataKind.CURRENT
    assert batch.provenance.request["volume_unit"] == "usd"
    assert [c.open_time.minute for c in batch.candles] == [0, 5, 10, 15, 20]  # oldest first
    assert [c.closed for c in batch.candles] == [True, True, True, True, False]  # 18:20 bar still open
    # numbers keep the provider's exact decimal text (no binary float artefacts)
    assert batch.candles[3].close == Decimal("3.310323227757079e-06")
    assert batch.candles[0].open == Decimal("3.292086369263938e-06")
    assert batch.parse_issues == ()

    report = validate_batch(batch, RECORDED_AT).report
    assert report.status is DataStatus.OK and report.candles_out == 4


def test_builds_expected_url_and_version_header():
    gt, transport = provider(HttpResponse(200, RECORDED.read_bytes()))
    end = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
    gt_default = GeckoTerminalProvider(transport=transport)  # header set by the default client
    assert "version=20230203" in gt_default._client._headers["Accept"]

    url = gt.build_url(BONK, Interval.H4, 1000, end)
    parsed = urlparse(url)
    assert parsed.path == "/api/v2/networks/solana/pools/5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9/ohlcv/hour"
    q = parse_qs(parsed.query)
    assert q["aggregate"] == ["4"] and q["limit"] == ["1000"] and q["currency"] == ["usd"]
    assert q["include_empty_intervals"] == ["true"] and q["before_timestamp"] == [str(int(end.timestamp()))]


@pytest.mark.parametrize("symbol", ["BTC/USDT", "eth:5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9",
                                    "solana:../../networks", "solana:0OIl" + "1" * 30])
def test_rejects_bad_symbols(symbol):
    with pytest.raises(ValueError):
        split_symbol(symbol)


def test_limit_bounds():
    gt, _ = provider()
    with pytest.raises(ValueError):
        gt.build_url(BONK, Interval.M1, 1001, None)


def test_rate_limit_exhausted_becomes_provider_error():
    r429 = HttpResponse(429, b'{"status":{"error_code":429}}', {"retry-after": "0"})
    gt, transport = provider(r429, r429)
    with pytest.raises(ProviderError) as err:
        gt.fetch_candles(BONK, Interval.M5)
    assert err.value.reason == "rate_limited" and len(transport.urls) == 2


def test_timeout_exhausted_becomes_provider_error():
    gt, _ = provider(TimeoutError("t"), TimeoutError("t"))
    with pytest.raises(ProviderError) as err:
        gt.fetch_candles(BONK, Interval.M5)
    assert err.value.reason == "timeout"


@pytest.mark.parametrize("raw,reason", [
    (b"<html>cloudflare</html>", "bad_payload"),
    (b'{"data": {}}', "bad_payload"),
    (b'{"data": {"attributes": {"ohlcv_list": {}}}}', "bad_payload"),
    (body([]), "empty"),
])
def test_bad_or_empty_payloads(raw, reason):
    gt, _ = provider(HttpResponse(200, raw))
    with pytest.raises(ProviderError) as err:
        gt.fetch_candles(BONK, Interval.M5)
    assert err.value.reason == reason


def test_bad_rows_become_parse_issues_not_crashes():
    t = 1791828000
    rows = [
        [t, 1.0, 1.1, 0.9, 1.0, 5.0],
        [t + 300, 0, 1.1, 0.9, 1.0, 5.0],        # zero price
        [t + 600, 1.0, 1.1, 0.9, 1.0, -1.0],     # negative volume
        [t + 900, 1.0, 0.95, 0.9, 1.0, 5.0],     # high below open
        [t + 1200, "abc", 1, 1, 1, 1],           # not a number
        [t + 1500, 1.0, 1.1],                    # short row
    ]
    gt, _ = provider(HttpResponse(200, body(rows)))
    batch = gt.fetch_candles(BONK, Interval.M5).batch
    assert len(batch.candles) == 1
    assert [i.code for i in batch.parse_issues] == [
        IssueCode.NONPOSITIVE_PRICE, IssueCode.NEGATIVE_VOLUME, IssueCode.INCONSISTENT_OHLC,
        IssueCode.MALFORMED, IssueCode.MALFORMED]
    report = validate_batch(batch, datetime.fromtimestamp(t + 400, tz=timezone.utc)).report
    assert report.status is DataStatus.DEGRADED and report.candles_in == 6
