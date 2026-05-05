"""Small HTTP retry helpers for flaky public APIs (timeouts, 5xx)."""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

__all__ = ["requests_get_with_retries", "requests_post_with_retries"]


def _is_transient_http_status(status_code: int) -> bool:
    return status_code >= 500


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
        ),
    ):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return True
    return False


def _sleep_backoff(attempt: int, backoff_base: float) -> None:
    delay = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.35)
    time.sleep(delay)


def requests_get_with_retries(
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 0.5,
    log_name: str = "http_retry",
    session: Optional[requests.Session] = None,
    **kwargs: Any,
) -> requests.Response:
    """GET with retries on connection/timeout errors and HTTP 5xx."""
    sess = session or requests
    max_retries = max(1, int(max_retries))
    last_resp: Optional[requests.Response] = None
    log = logging.getLogger(log_name)

    for attempt in range(1, max_retries + 1):
        try:
            resp = sess.get(url, **kwargs)
            last_resp = resp
            if _is_transient_http_status(resp.status_code):
                if attempt < max_retries:
                    log.warning(
                        "%s GET transient status=%s attempt %s/%s — retrying",
                        log_name,
                        resp.status_code,
                        attempt,
                        max_retries,
                    )
                    _sleep_backoff(attempt, backoff_base)
                    continue
            return resp
        except Exception as exc:
            if attempt < max_retries and _is_transient_exception(exc):
                log.warning(
                    "%s GET attempt %s/%s failed: %s — retrying",
                    log_name,
                    attempt,
                    max_retries,
                    exc,
                )
                _sleep_backoff(attempt, backoff_base)
                continue
            raise

    assert last_resp is not None
    return last_resp


def requests_post_with_retries(
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 0.5,
    log_name: str = "http_retry",
    session: Optional[requests.Session] = None,
    **kwargs: Any,
) -> requests.Response:
    """POST with retries on connection/timeout errors and HTTP 5xx."""
    sess = session or requests
    max_retries = max(1, int(max_retries))
    last_resp: Optional[requests.Response] = None
    log = logging.getLogger(log_name)

    for attempt in range(1, max_retries + 1):
        try:
            resp = sess.post(url, **kwargs)
            last_resp = resp
            if _is_transient_http_status(resp.status_code):
                if attempt < max_retries:
                    log.warning(
                        "%s POST transient status=%s attempt %s/%s — retrying",
                        log_name,
                        resp.status_code,
                        attempt,
                        max_retries,
                    )
                    _sleep_backoff(attempt, backoff_base)
                    continue
            return resp
        except Exception as exc:
            if attempt < max_retries and _is_transient_exception(exc):
                log.warning(
                    "%s POST attempt %s/%s failed: %s — retrying",
                    log_name,
                    attempt,
                    max_retries,
                    exc,
                )
                _sleep_backoff(attempt, backoff_base)
                continue
            raise

    assert last_resp is not None
    return last_resp
