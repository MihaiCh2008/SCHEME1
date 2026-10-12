"""Minimal HTTP client with pacing and bounded retries.

Uses only the standard library. Tests inject a fake ``Transport`` and a fake clock,
so no test touches the network or really sleeps.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout_s: float) -> HttpResponse:
        """Return the response for any HTTP status. Raise ``TimeoutError`` or ``OSError`` on network failure."""


class UrllibTransport:
    def get(self, url: str, headers: dict[str, str], timeout_s: float) -> HttpResponse:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return HttpResponse(resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as err:
            return HttpResponse(err.code, err.read(), {k.lower(): v for k, v in (err.headers or {}).items()})
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError):
                raise err.reason
            raise OSError(str(err.reason)) from err


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class HttpError(Exception):
    """The request failed after all allowed attempts. ``reason`` is short and stable for logs."""

    def __init__(self, reason: str, detail: str, status: int | None = None, attempts: int = 0):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.status = status
        self.attempts = attempts


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_s: float = 2.0  # waits 2, 4, 8 ... seconds
    rate_limit_backoff_s: float = 60.0  # wait after a 429; GeckoTerminal sends "retry-after: 0"
    timeout_s: float = 15.0


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class PacedClient:
    """Spaces requests at least ``min_interval_s`` apart and retries transient failures a bounded number of times."""

    def __init__(
        self,
        transport: Transport,
        min_interval_s: float,
        retry: RetryPolicy = RetryPolicy(),
        clock: Clock | None = None,
        headers: dict[str, str] | None = None,
        on_attempt: Callable[[str, int, str], None] | None = None,
    ):
        self._transport = transport
        self._min_interval_s = min_interval_s
        self._retry = retry
        self._clock = clock or SystemClock()
        self._headers = dict(headers or {})
        self._last_request: float | None = None
        self._on_attempt = on_attempt

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval_s - (self._clock.monotonic() - self._last_request)
            if wait > 0:
                self._clock.sleep(wait)
        self._last_request = self._clock.monotonic()

    def get(self, url: str) -> HttpResponse:
        last: HttpError | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            self._pace()
            try:
                resp = self._transport.get(url, self._headers, self._retry.timeout_s)
            except TimeoutError as err:
                last = HttpError("timeout", str(err) or "request timed out", attempts=attempt)
                wait = self._retry.backoff_s * 2 ** (attempt - 1)
            except OSError as err:
                last = HttpError("network", str(err), attempts=attempt)
                wait = self._retry.backoff_s * 2 ** (attempt - 1)
            else:
                if 200 <= resp.status < 300:
                    return resp
                if resp.status not in RETRYABLE_STATUS:
                    raise HttpError("http_status", _snippet(resp.body), status=resp.status, attempts=attempt)
                if resp.status == 429:
                    last = HttpError("rate_limited", _snippet(resp.body), status=429, attempts=attempt)
                    wait = max(self._retry.rate_limit_backoff_s, _retry_after(resp))
                else:
                    last = HttpError("server_error", _snippet(resp.body), status=resp.status, attempts=attempt)
                    wait = self._retry.backoff_s * 2 ** (attempt - 1)
            if self._on_attempt:
                self._on_attempt(url, attempt, last.reason)
            if attempt < self._retry.max_attempts:
                self._clock.sleep(wait)
        assert last is not None
        raise last


def _retry_after(resp: HttpResponse) -> float:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "0")))
    except ValueError:
        return 0.0


def _snippet(body: bytes, limit: int = 200) -> str:
    return body[:limit].decode("utf-8", errors="replace")
