"""Pinned original Parquet acquisition with an independent verified checkpoint."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from bulk_common import Blob, Err, Ok, canonical_bytes

_BASE = "https://huggingface.co/datasets/harvard-lil/cold-cases/resolve/"
_HOSTS = frozenset({"huggingface.co", "us.aws.cdn.hf.co", "cas-bridge.xethub.hf.co", "cdn-lfs.huggingface.co", "cdn-lfs-us-1.hf.co", "cdn-lfs.hf.co"})
_SAFE_HEADERS = frozenset({"etag", "last-modified", "content-type", "content-length"})
_RECEIPT_SCHEMA = "cap.joined-source-acquisition/v1"


@dataclass(frozen=True)
class SourceSpec:
    revision: str
    path: str
    expected_length: int
    publisher_sha256: str

    @property
    def url(self):
        return _BASE + self.revision + "/" + self.path

    def descriptor(self):
        _validate_spec(self)
        return {"kind": "cold-parquet-shard", "revision": self.revision, "path": self.path,
                "url": self.url, "expected_length": self.expected_length,
                "publisher_sha256": self.publisher_sha256}


APPROVED_SOURCE = SourceSpec(
    "5d8d0d8457ef63b6463af9737da21d3badd924ad",
    "part-00030-bd7b5d85-e1bd-440b-8817-95ec234884f3-c000.gz.parquet",
    959035282,
    "f69470bb88733f2ecfafe0c7a3f4b14eb5bc828155c339310d04c9765bdb5070",
)


def _validate_spec(source):
    if not isinstance(source, SourceSpec) or not re.fullmatch(r"[a-f0-9]{40}", source.revision):
        raise ValueError("Invalid source revision")
    if not isinstance(source.path, str) or not re.fullmatch(r"part-[0-9]{5}-[a-f0-9-]{36}-c000\.gz\.parquet", source.path):
        raise ValueError("Invalid source path")
    if type(source.expected_length) is not int or not 8 <= source.expected_length <= 1024**3:
        raise ValueError("Source exceeds approved acquisition bound")
    Blob(source.publisher_sha256, source.expected_length)


def _url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid public source URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in _HOSTS:
        raise ValueError("Source redirect host is not approved")
    if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError("Invalid source redirect authority")
    return value


class _NoRedirect(HTTPRedirectHandler):
    # urllib requires this callback signature to disable its implicit redirect path.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request):
    try:
        return build_opener(_NoRedirect()).open(request, timeout=30)
    except HTTPError as response:
        if response.code in (301, 302, 303, 307, 308):
            return response
        raise ValueError("Source HTTP request failed") from None


def _headers(response):
    pairs = list(response.headers.items())
    if len(pairs) > 64 or sum(len(k) + len(v) for k, v in pairs) > 32768:
        raise ValueError("Source response headers exceed bounds")
    result = {}
    for key, value in pairs:
        key = key.lower()
        if key in result or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Duplicate or malformed source headers")
        result[key] = value
    return result


def _response(source, opener):
    url = source.url
    for _ in range(6):
        response = opener(Request(_url(url), headers={"Accept-Encoding": "identity", "User-Agent": "civic-source-joined/1"}))
        try:
            headers = _headers(response)
            if response.status == 200:
                return response, headers
            if response.status not in (301, 302, 303, 307, 308) or "location" not in headers:
                raise ValueError("Unexpected source HTTP status")
            url = _url(urljoin(url, headers["location"]))
        except Exception:
            response.close()
            raise
        response.close()
    raise ValueError("Source redirects exceed bound")


def _validate_response(headers, source):
    value = headers.get("content-length", "")
    if not re.fullmatch(r"[0-9]{1,12}", value) or int(value) != source.expected_length:
        raise ValueError("Source Content-Length mismatch")
    if headers.get("content-encoding", "identity").lower() != "identity" or "content-range" in headers:
        raise ValueError("Source requires complete unencoded bytes")


def _copy(response, output, source, store):
    digest, total, started = hashlib.sha256(), 0, time.monotonic()
    while True:
        if time.monotonic() - started > 1800:
            raise ValueError("Source acquisition exceeded wall limit")
        chunk = response.read(min(65536, source.expected_length - total + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes) or total + len(chunk) > source.expected_length:
            raise ValueError("Source body exceeds publisher length")
        capacity = store.ensure_capacity(len(chunk))
        if isinstance(capacity, Err):
            raise ValueError(capacity.error)
        output.write(chunk)
        total += len(chunk)
        digest.update(chunk)
    if total != source.expected_length or digest.hexdigest() != source.publisher_sha256:
        raise ValueError("Source body hash or length mismatch")
    output.flush()
    os.fsync(output.fileno())


def _stage_download(store, source, opener):
    root = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.open("staging", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    finally:
        os.close(root)
    name = "joined-" + uuid4().hex
    created = False
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, "wb") as output:
            identity = os.fstat(output.fileno())
            response, headers = _response(source, opener)
            with response:
                _validate_response(headers, source)
                _copy(response, output, source, store)
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("Source staging identity changed")
        blob = Blob(source.publisher_sha256, source.expected_length)
        adopted = store.adopt(store.staging / name, blob)
        if isinstance(adopted, Err):
            raise ValueError(adopted.error)
        return {**source.descriptor(), "blob": blob.to_dict(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "headers": {key: value for key, value in headers.items() if key in _SAFE_HEADERS}}
    finally:
        try:
            if created:
                os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def download_source(store, source=APPROVED_SOURCE, *, opener=None):
    try:
        _validate_spec(source)
        if opener is None and source != APPROVED_SOURCE:
            raise ValueError("Production transport only permits the approved shard")
        capacity = store.ensure_capacity(source.expected_length + 65536)
        if isinstance(capacity, Err):
            return capacity
        return Ok(_stage_download(store, source, opener or _open))
    except (ValueError, OSError) as error:
        return Err(str(error) if isinstance(error, ValueError) else "Source acquisition transport or filesystem failure")
    except Exception:
        return Err("Source acquisition transport failure")


def validate_descriptor(value, source=APPROVED_SOURCE):
    if not isinstance(value, dict) or set(value) != set(source.descriptor()) | {"blob", "acquired_at", "headers"}:
        raise ValueError("Invalid source acquisition descriptor")
    if any(value[key] != expected for key, expected in source.descriptor().items()):
        raise ValueError("Source checkpoint publisher identity mismatch")
    if value["blob"] != Blob(source.publisher_sha256, source.expected_length).to_dict():
        raise ValueError("Source checkpoint blob identity mismatch")
    stamp = value["acquired_at"]
    if not isinstance(stamp, str) or len(stamp) > 64 or datetime.fromisoformat(stamp).utcoffset() is None:
        raise ValueError("Invalid source acquisition timestamp")
    headers = value["headers"]
    if not isinstance(headers, dict) or set(headers) - _SAFE_HEADERS or len(canonical_bytes(headers)) > 32768:
        raise ValueError("Invalid retained source headers")
    if any(not isinstance(v, str) or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in headers.values()):
        raise ValueError("Invalid retained source header value")
    return value


def _acquire(store, source, fetcher):
    _validate_spec(source)
    pointer = store.root / "current-joined-source.json"
    if os.path.lexists(pointer):
        existing = store.load("joined-source")
        if isinstance(existing, Err):
            return existing
        payload = existing.value
        if set(payload) != {"schema_version", "acquired_at", "source"} or payload["schema_version"] != _RECEIPT_SCHEMA:
            raise ValueError("Invalid source acquisition checkpoint")
        descriptor = validate_descriptor(payload["source"], source)
        if payload["acquired_at"] != descriptor["acquired_at"]:
            raise ValueError("Source checkpoint timestamp mismatch")
        return Ok(descriptor)
    downloaded = fetcher(store, source)
    if isinstance(downloaded, Err):
        return downloaded
    descriptor = validate_descriptor(downloaded.value, source)
    blob = Blob(**descriptor["blob"])
    verified = store.verify(blob)
    if isinstance(verified, Err):
        return verified
    payload = {"schema_version": _RECEIPT_SCHEMA, "acquired_at": descriptor["acquired_at"], "source": descriptor}
    accepted = store.accept("joined-source", payload, [blob])
    return accepted if isinstance(accepted, Err) else Ok(descriptor)


def acquire_source(store, *, source=APPROVED_SOURCE, fetcher=download_source):
    try:
        return _acquire(store, source, fetcher)
    except Exception as error:
        return Err(str(error) if isinstance(error, ValueError) else "Source checkpoint operation failed")
