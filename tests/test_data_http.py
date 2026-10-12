import pytest

from scheme1.data.http import HttpError, HttpResponse, PacedClient, RetryPolicy
from data_helpers import FakeClock, ScriptedTransport

OK = HttpResponse(200, b"{}")


def client(transport, clock, min_interval_s=0.0, **kw):
    return PacedClient(transport, min_interval_s=min_interval_s, retry=RetryPolicy(**kw), clock=clock)


def test_paces_consecutive_requests():
    clock = FakeClock()
    c = client(ScriptedTransport(OK, OK), clock, min_interval_s=6.5)
    c.get("u1")
    clock.t += 2.0  # caller spent 2s between calls
    c.get("u2")
    assert clock.sleeps == [pytest.approx(4.5)]


def test_retries_timeout_with_exponential_backoff_then_succeeds():
    clock = FakeClock()
    t = ScriptedTransport(TimeoutError("slow"), OSError("reset"), OK)
    assert client(t, clock).get("u") == OK
    assert clock.sleeps == [2.0, 4.0]
    assert len(t.urls) == 3


def test_rate_limit_waits_at_least_60s_even_with_retry_after_zero():
    clock = FakeClock()
    t = ScriptedTransport(HttpResponse(429, b'{"status":{"error_code":429}}', {"retry-after": "0"}), OK)
    client(t, clock).get("u")
    assert clock.sleeps == [60.0]


def test_honours_longer_retry_after():
    clock = FakeClock()
    t = ScriptedTransport(HttpResponse(429, b"", {"retry-after": "90"}), OK)
    client(t, clock).get("u")
    assert clock.sleeps == [90.0]


def test_client_error_is_not_retried():
    t = ScriptedTransport(HttpResponse(400, b'{"errors":[{"title":"Invalid aggregate"}]}'))
    with pytest.raises(HttpError) as err:
        client(t, FakeClock()).get("u")
    assert err.value.reason == "http_status" and err.value.status == 400 and len(t.urls) == 1


def test_gives_up_after_max_attempts():
    t = ScriptedTransport(*[HttpResponse(503, b"down")] * 3)
    clock = FakeClock()
    with pytest.raises(HttpError) as err:
        client(t, clock, max_attempts=3).get("u")
    assert err.value.reason == "server_error" and err.value.attempts == 3 and len(t.urls) == 3
    assert clock.sleeps == [2.0, 4.0]  # no sleep after the last try
