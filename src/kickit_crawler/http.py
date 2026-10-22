"""Shared HTTP client with polite defaults and retry/backoff."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Overpass rejects (406) user agents containing "crawler" or default library names.
USER_AGENT = "kick-it-pitches/0.1 (+https://github.com/Mohamed-Moumni)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


def make_client(timeout: float = 240.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 5,
    backoff_s: float = 10.0,
    **kwargs: Any,
) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            resp = client.request(method, url, **kwargs)
            if resp.status_code not in RETRY_STATUSES:
                resp.raise_for_status()
                return resp.json()
            reason = f"HTTP {resp.status_code}"
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            reason = type(exc).__name__
        if attempt == attempts:
            raise RuntimeError(f"{method} {url} failed after {attempts} attempts ({reason})")
        wait = backoff_s * attempt
        log.warning("%s %s -> %s, retrying in %.0fs (%d/%d)", method, url, reason, wait, attempt, attempts)
        time.sleep(wait)
    raise AssertionError("unreachable")
