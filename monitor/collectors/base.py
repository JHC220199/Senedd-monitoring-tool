"""Shared HTTP plumbing for collectors.

Deliberately conservative. We are a guest on public infrastructure funded by
Welsh taxpayers, and the Senedd publishes this data under the Open Government
Licence on the understanding that it is used responsibly. Concretely that means:

* An honest, identifying User-Agent with a contact address. If we ever cause a
  problem, whoever runs the Senedd's web estate should be able to email us
  rather than having to block us.
* A hard floor on request spacing, enforced globally rather than per-collector.
* Retry with exponential backoff on transient failures only — never on 4xx.
* Conditional requests (ETag / If-Modified-Since) so re-runs are cheap for the
  publisher as well as for us.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


log = logging.getLogger(__name__)

# IMPORTANT: do NOT add "python-requests", "python-urllib", "curl", "scrapy" or
# any similar library token to this string.
#
# This cost real debugging time. The original value ended in "python-requests",
# and that single token caused CloudFront's managed bot rules to return HTTP 403
# on senedd.wales and senedd.cymru. Two collectors — Senedd Bills and the
# forward look — were reported as "blocked by a WAF" for days when the block was
# entirely self-inflicted. Verified 4 August 2026 against
# senedd.cymru/deddfwriaeth/:
#
#     "NRLA-PolicyMonitor/1.0 (... +mailto:policy@nrla.org.uk)"  -> 200
#     "NRLA-PolicyMonitor/1.0 python-requests"                   -> 403
#     "python-requests/2.31.0"                                   -> 403
#
# The string below stays honest and identifying — organisation name and a real
# contact address — so that anyone operating the Senedd's web estate can reach
# us rather than having to block us. It deliberately does NOT impersonate a
# browser: presenting a fake Mozilla/Chrome string would be evasion, which is
# both fragile and not how we want to behave towards public infrastructure.
USER_AGENT = (
    "NRLA-PolicyMonitor/1.0 "
    "(National Residential Landlords Association; "
    "+mailto:policy@nrla.org.uk)"
)

# Minimum seconds between requests to the same host. The Senedd's estate is not
# large and there is no published rate limit, so we set our own and stay well
# inside anything anyone would consider reasonable.
MIN_INTERVAL = 1.5


@dataclass
class Fetcher:
    cache_dir: Path | None = None
    min_interval: float = MIN_INTERVAL
    timeout: int = 60
    max_retries: int = 3
    session: requests.Session = field(default_factory=requests.Session)
    _last_request: dict[str, float] = field(default_factory=dict)
    _etags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-GB,en;q=0.9,cy;q=0.8",
        })
        if self.cache_dir:
            Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    # -- politeness -------------------------------------------------------
    def _throttle(self, url: str) -> None:
        host = requests.utils.urlparse(url).netloc
        last = self._last_request.get(host, 0.0)
        wait = self.min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    # -- fetching ---------------------------------------------------------
    def get(self, url: str, params: dict[str, Any] | None = None,
            allow_cached: bool = True) -> requests.Response | None:
        """GET a URL, or return None if it could not be retrieved.

        Returning None rather than raising is intentional: one unreachable
        source must never stop the rest of the run. gov.wales, for instance,
        sits behind a CloudFront WAF that rejects requests from datacentre IP
        ranges — a run from a cloud host will lose that source but must still
        deliver everything from the Senedd.
        """
        headers: dict[str, str] = {}
        if allow_cached and url in self._etags:
            headers["If-None-Match"] = self._etags[url]

        for attempt in range(1, self.max_retries + 1):
            self._throttle(url)
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("fetch error (attempt %d/%d) %s: %s",
                            attempt, self.max_retries, url, exc)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 304:
                log.info("unchanged since last run: %s", url)
                return None

            if resp.status_code == 200:
                etag = resp.headers.get("ETag")
                if etag:
                    self._etags[url] = etag
                return resp

            # 403/404/410 are settled answers; retrying is pointless and rude.
            if resp.status_code in (401, 403, 404, 410):
                log.warning("blocked or absent (%s): %s", resp.status_code, url)
                if resp.status_code == 403:
                    log.warning(
                        "  403 means a WAF rejected this request. Check, in order: "
                        "(1) the User-Agent contains no library token such as "
                        "'python-requests' — that alone triggers CloudFront bot "
                        "rules on senedd.wales and senedd.cymru; (2) whether the "
                        "host blocks datacentre IPs, which gov.wales does "
                        "regardless of User-Agent — use the mailbox route for it."
                    )
                return None

            if resp.status_code in (429, 500, 502, 503, 504):
                back_off = 2 ** attempt
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    back_off = max(back_off, int(retry_after))
                log.warning("transient %s on %s, backing off %ss",
                            resp.status_code, url, back_off)
                time.sleep(back_off)
                continue

            log.warning("unexpected %s on %s", resp.status_code, url)
            return None

        log.error("giving up on %s after %d attempts", url, self.max_retries)
        return None

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str | None:
        resp = self.get(url, params)
        if resp is None:
            return None
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes | None:
        resp = self.get(url, params)
        return resp.content if resp is not None else None


class Collector:
    """Base class. Subclasses implement `collect()` and yield `Item`s."""

    name = "collector"
    source_kind = "other"

    def __init__(self, fetcher: Fetcher | None = None, **options: Any) -> None:
        self.fetcher = fetcher or Fetcher()
        self.options = options
        self.errors: list[str] = []

    def collect(self):  # pragma: no cover - interface
        raise NotImplementedError

    def note_error(self, message: str) -> None:
        """Record a non-fatal problem so the run report can surface it.

        A monitoring system that silently returns fewer results when a source
        breaks is worse than no system, because the team will assume a quiet
        week rather than a broken feed. Every collector failure must be visible
        on the dashboard.
        """
        log.error("[%s] %s", self.name, message)
        self.errors.append(message)
