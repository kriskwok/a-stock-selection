"""Shared resilient HTTP access and data-source provenance tracking."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

import requests


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
EASTMONEY_MIN_INTERVAL = 1.5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SourceUnavailable(RuntimeError):
    """Raised when a source is unavailable or its circuit is open."""


@dataclass
class SourceEvent:
    source: str
    url: str
    timestamp: str
    status: str
    http_status: int | None = None
    fallback: bool = False
    error: str = ""
    elapsed_ms: int = 0


class ResilientHttpClient:
    """HTTP client with Eastmoney throttling, retries, circuit breaking and trace."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: int = 15,
        retries: int = 2,
        eastmoney_min_interval: float = EASTMONEY_MIN_INTERVAL,
        sleeper: Callable[[float], None] = time.sleep,
        randomizer: Callable[[float, float], float] = random.uniform,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.timeout = timeout
        self.retries = retries
        self.eastmoney_min_interval = eastmoney_min_interval
        self.sleeper = sleeper
        self.randomizer = randomizer
        self._last_eastmoney_call = 0.0
        self._lock = threading.Lock()
        self._circuit_open: set[str] = set()
        self._failure_counts: dict[str, int] = {}
        self.events: list[SourceEvent] = []

    @staticmethod
    def source_name(url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "eastmoney" in host or "dfcfw" in host:
            return "东方财富"
        if "10jqka" in host or "hexin" in host:
            return "同花顺"
        if "gtimg" in host or "qq.com" in host:
            return "腾讯财经"
        if "cninfo" in host:
            return "巨潮资讯"
        if "sina" in host:
            return "新浪财经"
        if "baidu" in host:
            return "百度股市通"
        if "cls.cn" in host:
            return "财联社"
        return host or "unknown"

    @staticmethod
    def _is_eastmoney(url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return "eastmoney" in host or "dfcfw" in host

    def _throttle(self, url: str) -> None:
        if not self._is_eastmoney(url):
            return
        with self._lock:
            wait = self.eastmoney_min_interval - (time.monotonic() - self._last_eastmoney_call)
            if wait > 0:
                self.sleeper(wait + self.randomizer(0.1, 0.35))
            self._last_eastmoney_call = time.monotonic()

    @staticmethod
    def _is_remote_disconnect(error: BaseException) -> bool:
        """Detect the shared Eastmoney connection-reset failure mode.

        A ``RemoteDisconnected`` response is not a stock-specific failure: push2 and
        datacenter normally fail together when the upstream blocks or resets the
        current network route.  Retrying once is useful for a transient close, but
        continuing through every endpoint/candidate only delays a properly degraded
        report.
        """
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "remotedisconnected",
                "remote end closed connection",
                "connection reset",
                "connection aborted",
            )
        )

    def _record(
        self,
        source: str,
        url: str,
        status: str,
        started: float,
        http_status: int | None = None,
        fallback: bool = False,
        error: str = "",
    ) -> None:
        self.events.append(
            SourceEvent(
                source=source,
                url=url,
                timestamp=datetime.now().isoformat(timespec="seconds"),
                status=status,
                http_status=http_status,
                fallback=fallback,
                error=error[:240],
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        source: str | None = None,
        fallback: bool = False,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        source = source or self.source_name(url)
        circuit_key = "eastmoney" if self._is_eastmoney(url) else urlparse(url).netloc.lower()
        if circuit_key in self._circuit_open:
            raise SourceUnavailable(f"{source} 熔断已开启")

        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            try:
                response = self.session.request(method, url, timeout=timeout or self.timeout, **kwargs)
                if response.status_code == 403:
                    self._circuit_open.add(circuit_key)
                    self._record(source, url, "circuit_open", started, 403, fallback, "HTTP 403")
                    raise SourceUnavailable(f"{source} HTTP 403，已熔断")
                if response.status_code in RETRYABLE_STATUS and attempt < self.retries:
                    self.sleeper((0.6 * (2**attempt)) + self.randomizer(0.05, 0.2))
                    continue
                response.raise_for_status()
                self._failure_counts[circuit_key] = 0
                self._record(source, url, "success", started, response.status_code, fallback)
                return response
            except SourceUnavailable:
                raise
            except (requests.RequestException, TimeoutError) as exc:
                last_error = exc
                remote_disconnect = self._is_eastmoney(url) and self._is_remote_disconnect(exc)
                # Retain one retry for a transient route close.  Once it repeats,
                # all Eastmoney hosts share one circuit and later stages can use
                # their independent sources or report the field as unverified.
                retry_limit = min(self.retries, 1) if remote_disconnect else self.retries
                if attempt < retry_limit:
                    self.sleeper((0.6 * (2**attempt)) + self.randomizer(0.05, 0.2))
                    continue
                failures = self._failure_counts.get(circuit_key, 0) + 1
                self._failure_counts[circuit_key] = failures
                status = "failed"
                if remote_disconnect or failures >= 3:
                    self._circuit_open.add(circuit_key)
                    status = "circuit_open"
                self._record(source, url, status, started, error=str(exc), fallback=fallback)
                break
        raise SourceUnavailable(f"{source} 请求失败: {last_error}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs).json()

    def get_text(self, url: str, encoding: str | None = None, **kwargs: Any) -> str:
        response = self.request("GET", url, **kwargs)
        if encoding:
            response.encoding = encoding
        return response.text

    def call_with_fallback(
        self,
        primary: Callable[[], Any],
        fallback: Callable[[], Any],
        label: str,
    ) -> tuple[Any, str]:
        try:
            return primary(), "primary"
        except Exception as primary_error:
            try:
                result = fallback()
                self.events.append(
                    SourceEvent(
                        source=label,
                        url="fallback",
                        timestamp=datetime.now().isoformat(timespec="seconds"),
                        status="fallback_success",
                        fallback=True,
                        error=str(primary_error)[:240],
                    )
                )
                return result, "fallback"
            except Exception as fallback_error:
                raise SourceUnavailable(
                    f"{label} 主源与备源均失败: {primary_error}; {fallback_error}"
                ) from fallback_error

    def trace(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self.events]

    def reset_circuits(self) -> None:
        self._circuit_open.clear()
        self._failure_counts.clear()


_DEFAULT_CLIENT: ResilientHttpClient | None = None


def get_default_client() -> ResilientHttpClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = ResilientHttpClient()
    return _DEFAULT_CLIENT


def set_default_client(client: ResilientHttpClient | None) -> None:
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client
