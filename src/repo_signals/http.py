"""A small, strict HTTPS JSON client built on the standard library: GET and POST.

- https only, and only to hosts in the allowlist given at construction;
- redirects are never followed (a 3xx is an error);
- the response body is capped (1 MB by default);
- every call has a timeout;
- proxies from the environment are ignored unless proxies are passed explicitly;
- every failure is a ``ProviderError`` carrying a code only: never the URL, the
  query string, a header or the body.

The transport is injectable, so tests use a fake and never touch a network.
"""
import json
import math
import socket
import ssl
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from typing import NamedTuple, Protocol
from urllib.parse import urlencode, urlsplit

from .providers.base import ProviderError

MAX_BODY_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 120.0
MAX_REQUEST_BYTES = 64 * 1024
_CHUNK = 64 * 1024


class RawResponse(NamedTuple):
    status: int
    headers: dict  # lowercased header names
    body: bytes


class Transport(Protocol):
    """GET calls pass four arguments; POST calls add ``method="POST"`` and ``body``."""

    def __call__(self, url: str, headers: dict, timeout: float, max_bytes: int,
                 method: str = "GET", body: bytes | None = None) -> RawResponse:
        ...


class JsonResponse(tuple):
    """``(status, data)``. Also carries ``headers`` (lowercased names) for Retry-After and the like.

    ``data`` is the parsed JSON, or None when a non-2xx response had no JSON body.
    """

    def __new__(cls, status, data, headers):
        self = super().__new__(cls, (status, data))
        self.headers = headers
        return self

    @property
    def status(self):
        return self[0]

    @property
    def data(self):
        return self[1]

    def __repr__(self):
        return f"JsonResponse(status={self[0]})"


def _clean(text):
    return isinstance(text, str) and text != "" and all(
        0x20 < ord(c) < 0x7f for c in text)


def _check_timeout(timeout):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT):
        raise ValueError(f"timeout must be a number of seconds above 0 and at most {MAX_TIMEOUT:g}")
    return float(timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib then raises HTTPError with the 3xx status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener(proxies, https_handler=None):
    """An opener that uses only ``proxies`` (never the environment's) and follows no redirect."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        _NoRedirect(),
        https_handler or urllib.request.HTTPSHandler(context=ssl.create_default_context()))


class UrllibTransport:
    """The real transport. Proxies are ignored unless ``proxies`` is given explicitly."""

    def __init__(self, proxies=None, opener=None):
        if proxies is not None and (not isinstance(proxies, dict) or not all(
                k in ("http", "https") and _clean(v) for k, v in proxies.items())):
            raise ValueError("proxies must map 'http' or 'https' to a proxy URL")
        self.proxies = dict(proxies) if proxies else {}
        self._opener = opener if opener is not None else build_opener(self.proxies)

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        if not url.startswith("https://"):
            raise ProviderError("insecure_scheme")
        if method not in ("GET", "POST") or (body is not None and not isinstance(body, bytes)):
            raise ProviderError("bad_method")
        deadline = time.monotonic() + timeout
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            try:
                resp = self._opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                resp = e
            with resp:
                status = getattr(resp, "status", None) or resp.code
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                body = b""
                while len(body) <= max_bytes:
                    if time.monotonic() > deadline:
                        raise ProviderError("timeout")
                    chunk = resp.read(min(_CHUNK, max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body += chunk
        except ProviderError:
            raise
        except (TimeoutError, socket.timeout):
            raise ProviderError("timeout") from None
        except ssl.SSLError:
            raise ProviderError("tls_error") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise ProviderError("timeout") from None
            if isinstance(e.reason, ssl.SSLError):
                raise ProviderError("tls_error") from None
            raise ProviderError("network_error") from None
        except (OSError, HTTPException, ValueError):
            raise ProviderError("network_error") from None
        return RawResponse(status, hdrs, body)


class HttpClient:
    def __init__(self, allowed_hosts, *, transport=None, proxies=None,
                 max_body_bytes=MAX_BODY_BYTES):
        if isinstance(allowed_hosts, str) or not allowed_hosts or not all(
                _clean(h) and "/" not in h and ":" not in h for h in allowed_hosts):
            raise ValueError("allowed_hosts must be a non-empty collection of host names")
        if transport is not None and proxies is not None:
            raise ValueError("pass proxies to the transport, not alongside one")
        if (isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int)
                or not 0 < max_body_bytes <= MAX_BODY_BYTES):
            raise ValueError(f"max_body_bytes must be from 1 to {MAX_BODY_BYTES}")
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.max_body_bytes = max_body_bytes
        self._transport = transport if transport is not None else UrllibTransport(proxies)

    def _check_url(self, url):
        if not isinstance(url, str) or not _clean(url):
            raise ProviderError("bad_url")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise ProviderError("bad_url") from None
        if parts.scheme != "https":
            raise ProviderError("insecure_scheme")
        if (parts.hostname or "").lower() not in self.allowed_hosts:
            raise ProviderError("host_not_allowed")
        if "@" in parts.netloc or port not in (None, 443) or parts.query or parts.fragment:
            raise ProviderError("bad_url")

    def _check_headers(self, headers):
        headers = {} if headers is None else headers
        if not isinstance(headers, dict) or not all(
                _clean(k) and isinstance(v, str) and "\r" not in v and "\n" not in v
                for k, v in headers.items()):
            raise ProviderError("bad_header")
        return dict(headers)

    def _send(self, url, headers, timeout, body=None):
        kwargs = {} if body is None else {"method": "POST", "body": body}
        try:
            raw = self._transport(url, headers, timeout, self.max_body_bytes, **kwargs)
        except ProviderError:
            raise
        except Exception:  # a transport must not leak its exception, which may hold the URL
            raise ProviderError("network_error") from None
        if not isinstance(raw, RawResponse) or not isinstance(raw.status, int) \
                or not isinstance(raw.body, bytes) or not isinstance(raw.headers, dict):
            raise ProviderError("bad_transport_response")
        if 300 <= raw.status < 400:
            raise ProviderError("redirect_refused")
        if len(raw.body) > self.max_body_bytes:
            raise ProviderError("response_too_large")
        hdrs = {str(k).lower(): v for k, v in raw.headers.items()}
        try:
            data = json.loads(raw.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            if 200 <= raw.status < 300:
                raise ProviderError("malformed_json") from None
            data = None
        return JsonResponse(raw.status, data, hdrs)

    def get_json(self, url, params=None, headers=None, timeout=DEFAULT_TIMEOUT) -> JsonResponse:
        """GET ``url`` with ``params`` as the query string; return ``(status, parsed JSON)``.

        A 2xx body that is not JSON raises ``malformed_json``. A non-2xx body that is not
        JSON gives ``data`` None, so the caller can still map the status.
        """
        timeout = _check_timeout(timeout)
        self._check_url(url)
        params = {} if params is None else params
        if not isinstance(params, dict) or not all(
                isinstance(k, str) and isinstance(v, (str, int)) and not isinstance(v, bool)
                for k, v in params.items()):
            raise ProviderError("bad_params")
        headers = self._check_headers(headers)
        full = url + ("?" + urlencode(params) if params else "")
        return self._send(full, headers, timeout)

    def post_json(self, url, payload, headers=None, timeout=DEFAULT_TIMEOUT) -> JsonResponse:
        """POST ``payload`` (a dict) as a JSON body; return ``(status, parsed JSON)``.

        Same rules as ``get_json``: https only, allowlisted host, no redirects, body cap,
        timeout, code-only errors. The URL may not carry a query string. The encoded
        request body is capped at MAX_REQUEST_BYTES.
        """
        timeout = _check_timeout(timeout)
        self._check_url(url)
        if not isinstance(payload, dict):
            raise ProviderError("bad_payload")
        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True,
                              allow_nan=False).encode("ascii")
        except (TypeError, ValueError, RecursionError):
            raise ProviderError("bad_payload") from None
        if len(body) > MAX_REQUEST_BYTES:
            raise ProviderError("payload_too_large")
        headers = self._check_headers(headers)
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
        return self._send(url, headers, timeout, body)
