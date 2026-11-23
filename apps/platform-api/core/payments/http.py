"""Shared bounded HTTP transport for payment provider adapters.

The client is synchronous, uses a short bounded timeout and retries at most a
fixed number of times on transient network failures or provider 5xx responses.
Test code may inject an ``httpx.MockTransport`` so no real outbound connection
is ever made.

The transport layer never logs request/response bodies and never persists
credentials; adapters resolve secret references server-side before building a
request, and only non-secret headers (signature, serial, nonce) are attached.
"""

from __future__ import annotations

import httpx

from core.payments.base import PaymentProviderError

DEFAULT_TIMEOUT = 10.0
DEFAULT_ATTEMPTS = 3
RETRYABLE_STATUS = {500, 502, 503, 504}


def build_client(
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.Client:
    """Create a sync client with a bounded timeout and no redirect follow."""

    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)


def send_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying transient failures a bounded number of times.

    Network/timeout errors and provider 5xx responses are retried; 4xx
    responses and successful responses return immediately.
    """

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            continue
        if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
            continue
        return response

    raise PaymentProviderError(
        "PAYMENT_PROVIDER_UNAVAILABLE",
        "The payment provider could not be reached.",
    ) from last_exc
