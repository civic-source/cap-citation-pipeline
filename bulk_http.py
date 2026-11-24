"""Bounded, redirect-free retrieval from explicitly allowed public sources."""
from dataclasses import dataclass
import http.client
import math
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bulk_common import Err, Ok, Result

DEFAULT_HOSTS = frozenset({"com-courtlistener-storage.s3.us-west-2.amazonaws.com", "raw.githubusercontent.com"})
MAX_BODY_BYTES = 32 * 1024**2


@dataclass(frozen=True)
class HttpArtifact:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class HttpPolicy:
    allowed_hosts: frozenset[str] = DEFAULT_HOSTS
    allow_http: bool = False


def _validate_budget(max_bytes, timeout):
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BODY_BYTES:
        raise ValueError("HTTP body budget must be between one byte and 32 MiB")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("HTTP timeout must be positive and no greater than 300 seconds")


def _validate_url(url, policy):
    if not isinstance(policy, HttpPolicy) or type(policy.allow_http) is not bool:
        raise ValueError("Invalid public source host policy")
    if not isinstance(policy.allowed_hosts, (set, frozenset)) or not all(isinstance(host, str) for host in policy.allowed_hosts):
        raise ValueError("Invalid public source host policy")
    if not isinstance(url, str) or len(url) > 32768 or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "#" in url:
        raise ValueError("Invalid source URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ({"https", "http"} if policy.allow_http else {"https"}) or parsed.hostname not in policy.allowed_hosts:
        raise ValueError("Source URL scheme or host is not allowed")
    if parsed.username is not None or parsed.password is not None or (parsed.scheme == "https" and parsed.port not in {None, 443}):
        raise ValueError("Source URL credentials or port are prohibited")


def _validate_headers(headers):
    if headers is not None and not isinstance(headers, dict):
        raise ValueError("Source headers must be a mapping")
    result = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", name) or not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid source request header")
        if name.lower() in result:
            raise ValueError("Duplicate source request header")
        result[name.lower()] = value
    result.setdefault("accept-encoding", "identity")
    result.setdefault("user-agent", "CivicSource-retained-source/1.0")
    return result


def _validate_request(url, max_bytes, timeout, policy, headers):
    _validate_budget(max_bytes, timeout)
    _validate_url(url, policy)
    return _validate_headers(headers)


def _response_headers(response):
    for name in ["Content-Length", "Content-Range", "Content-Encoding", "ETag"]:
        if len(response.headers.get_all(name, [])) > 1:
            raise ValueError("Ambiguous source response headers")
    return {name.lower(): value for name, value in response.headers.items()}


def _partial_length(headers, requested):
    actual = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
    intended = re.fullmatch(r"bytes=(\d+)-(\d+)", requested.get("range", ""))
    if not actual or not intended:
        raise ValueError("Unsolicited or malformed partial response")
    start, end, size = map(int, actual.groups())
    first, last = map(int, intended.groups())
    if start != first or end != min(last, size - 1) or not 0 <= start <= end < size or first > last:
        raise ValueError("Partial response differs from requested byte range")
    return end - start + 1


def _read_body(response, max_bytes, headers, requested):
    if response.status not in {200, 206} or (response.status == 200 and "range" in requested):
        raise ValueError(f"Unexpected source HTTP status {response.status}")
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("Encoded source response is not an exact retained artifact")
    length = headers.get("content-length")
    if length is not None and not re.fullmatch(r"[0-9]+", length):
        raise ValueError("Invalid source content length")
    expected = None if length is None else int(length)
    if response.status == 206:
        partial = _partial_length(headers, requested)
        if expected is not None and expected != partial:
            raise ValueError("Partial response content length mismatch")
        expected = partial
    if expected is not None and expected > max_bytes:
        raise ValueError("Source content length exceeds HTTP body budget")
    chunks, total = [], 0
    while chunk := response.read(min(65536, max_bytes - total + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("Source stream exceeds HTTP body budget")
        chunks.append(chunk)
    if expected is not None and total != expected:
        raise ValueError("Source stream length differs from advertised length")
    return b"".join(chunks)


def fetch_bytes(url: str, max_bytes: int, *, headers: dict[str, str] | None = None, timeout: float = 30,
                policy: HttpPolicy = HttpPolicy()) -> Result[HttpArtifact]:
    try:
        requested = _validate_request(url, max_bytes, timeout, policy, headers)
        opener = build_opener(_NoRedirect())
        try:
            response = opener.open(Request(url, headers=requested), timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            received = _response_headers(response)
            if response.status == 304:
                if not {"if-none-match", "if-modified-since"}.intersection(requested):
                    return Err("Unsolicited HTTP 304 response")
                return Ok(HttpArtifact(url, 304, received, b""))
            body = _read_body(response, max_bytes, received, requested)
            return Ok(HttpArtifact(url, response.status, received, body))
    except (OSError, ValueError, http.client.HTTPException) as error:
        return Err(f"Cannot fetch bounded source artifact: {error}")
