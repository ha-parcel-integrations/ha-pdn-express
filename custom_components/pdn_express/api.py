"""PDN Express public tracking API client."""
from __future__ import annotations

from typing import Any

import aiohttp

from .const import TRACKING_API_URL
from .parcels import warn_once

NOT_FOUND_PREFIX = "Sorry, your tracking attempt was not successful"

# The only failure string the service is known to use for a transient backend
# problem; anything else on an ``ok: false`` reply is a contract change.
RETRYABLE_ERROR = "upstream"


class PDNExpressApiError(Exception):
    """Raised when a PDN Express API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"PDN Express API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class PDNExpressApiClient:
    """Client for the keyless PDN Express tracking endpoint.

    One POST per tracking number, answered with HTTP 200 and::

        {"ok": true, "data": [{"orderNo": ..., "events": [...]}]}

    Several numbers in one request are read as a single unknown string, so
    callers must request them one by one.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking entry.

        Returns the entry for a parcel with events, or ``None`` when there is
        nothing usable to show (unknown number, an inconclusive notice, or an
        entry answering for another number), so the caller keeps its last-good
        data. A retryable ``upstream`` reply, any other failure envelope or a
        non-2xx status raises :class:`PDNExpressApiError`; network errors
        propagate as ``aiohttp.ClientError``.
        """
        body = {"action": "search", "orderNos": tracking_code, "locale": "en"}
        async with self._session.post(TRACKING_API_URL, json=body) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise PDNExpressApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise PDNExpressApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: tolerate a mislabelled JSON body rather
                # than letting aiohttp refuse to parse it.
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise PDNExpressApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise PDNExpressApiError("unexpected body (not a JSON object)")

        if payload.get("ok") is not True:
            error = payload.get("error")
            if error == RETRYABLE_ERROR:
                raise PDNExpressApiError(RETRYABLE_ERROR)
            warn_once(
                f"api-error:{error}",
                "PDN Express returned an unrecognised failure envelope "
                "(error=%s); keeping last-good data",
                error,
            )
            raise PDNExpressApiError(str(error or "unknown error envelope"))

        data = payload.get("data")
        entry = data[0] if isinstance(data, list) and data else None
        if not isinstance(entry, dict):
            raise PDNExpressApiError("unexpected body (no entry in data)")

        if entry.get("orderNo") != tracking_code:
            warn_once(
                "order-mismatch",
                "PDN Express answered for a different number than requested; "
                "ignoring the entry",
            )
            return None

        if entry.get("events"):
            return entry

        config = entry.get("statusCodeConfig")
        notice = config.get("lanName") if isinstance(config, dict) else None
        if isinstance(notice, str) and notice.startswith(NOT_FOUND_PREFIX):
            return None
        warn_once(
            "eventless-notice",
            "PDN Express returned an entry without events and with an "
            "unrecognised notice; keeping last-good data",
        )
        return None
