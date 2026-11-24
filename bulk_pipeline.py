"""Transactional capture and offline replay for explicitly scoped precedent data."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import re

from bulk_common import Blob, Err, Ok, Result, canonical_bytes
from bulk_http import fetch_bytes
from bulk_inventory import capture_inventory, parse_inventory_pages
from bulk_store import Store
from bulk_producer import (
    producer_identity,
    recipe_digest,
    INVENTORY_RECIPE_FILES,
)
from cold_projection import project_sample

MAX_SOURCE = 32 * 1024**2
MAX_PRODUCT = 64 * 1024**2


@dataclass(frozen=True)
class SourceSpec:
    revision: str
    file_date: str
    expected_length: int
    git_blob_sha1: str

    def __post_init__(self):
        if not re.fullmatch(r"[a-f0-9]{40}", self.revision) or not re.fullmatch(
            r"[a-f0-9]{40}", self.git_blob_sha1
        ):
            raise ValueError("Invalid pinned sample revision or Git blob")
        date.fromisoformat(self.file_date)
        if type(self.expected_length) is not int or not 0 < self.expected_length <= MAX_SOURCE:
            raise ValueError("Pinned sample exceeds source budget")

    @property
    def url(self):
        return f"https://raw.githubusercontent.com/harvard-lil/cold-cases-export/{self.revision}/sample.jsonl"

    def descriptor(self):
        return {
            "kind": "cold-jsonl-sample",
            "url": self.url,
            "revision": self.revision,
            "file_date": self.file_date,
            "expected_length": self.expected_length,
            "git_blob_sha1": self.git_blob_sha1,
        }


DEFAULT_SOURCE = SourceSpec(
    "5907c8453ffdfb5a6798d4fb8cb8faebdfc1025f",
    "2023-10-19",
    12907287,
    "d0a099b3b8ff136dfc084e8899908cac40622066",
)


def _blob(value):
    if not isinstance(value, dict) or set(value) != {"sha256", "byte_length"}:
        raise ValueError("Invalid retained artifact reference")
    return Blob(value["sha256"], value["byte_length"])


def current_manifest(store, kind):
    try:
        (store.root / f"current-{kind}.json").lstat()
    except FileNotFoundError:
        return Ok(None)
    except OSError as error:
        return Err(str(error))
    return store.load(kind)


def _utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_spec(value):
    if not isinstance(value, dict) or value.get("kind") != "cold-jsonl-sample":
        raise ValueError("Unexpected historical source kind")
    spec = SourceSpec(
        value["revision"], value["file_date"], value["expected_length"], value["git_blob_sha1"]
    )
    if value.get("url") != spec.url:
        raise ValueError("Historical source URL differs from pinned revision")
    return spec


def _check_source(raw, spec):
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if len(raw) != spec.expected_length or actual != spec.git_blob_sha1:
        raise ValueError("Source length or publisher Git blob identity mismatch")


def _retained_source(store, spec, descriptor):
    if _source_spec(descriptor) != spec:
        return Ok(None)
    raw = store.read(_blob(descriptor["blob"]), MAX_SOURCE)
    if isinstance(raw, Err):
        return raw
    _check_source(raw.value, spec)
    return Ok((descriptor, raw.value))


def _cached_source(store, spec, previous):
    if previous is not None:
        retained = _retained_source(store, spec, previous.get("source"))
        if isinstance(retained, Err) or retained.value is not None:
            return retained
    cached = current_manifest(store, "source")
    if isinstance(cached, Err) or cached.value is None:
        return cached
    if cached.value.get("schema_version") != "cap.source-acquisition/v1":
        return Err("Unexpected acquisition receipt")
    return _retained_source(store, spec, cached.value["source"])


def _acquire(store, spec, previous, fetcher):
    try:
        cached = _cached_source(store, spec, previous)
        if isinstance(cached, Err) or cached.value is not None:
            return cached
        fetched = fetcher(spec.url, MAX_SOURCE, headers={"Accept": "text/plain"})
        if isinstance(fetched, Err):
            return fetched
        artifact = fetched.value
        if artifact.url != spec.url or artifact.status != 200:
            raise ValueError("Unexpected pinned sample HTTP response")
        _check_source(artifact.body, spec)
        retained = retain_source(store, spec, artifact.body, artifact.headers)
        return retained if isinstance(retained, Err) else Ok((retained.value, artifact.body))
    except Exception as error:
        return Err(str(error))


def retain_source(store, spec, raw, headers):
    try:
        _check_source(raw, spec)
        retained = store.put(raw)
        if isinstance(retained, Err):
            return retained
        source = {
            **spec.descriptor(),
            "blob": retained.value.to_dict(),
            "observed_at": _utc(),
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
        }
        receipt = {"schema_version": "cap.source-acquisition/v1", "source": source}
        accepted = store.accept("source", receipt, [retained.value])
        return accepted if isinstance(accepted, Err) else Ok(source)
    except Exception as error:
        return Err(str(error))


def _snapshot_summary(identifier, payload, coverage):
    return {"snapshot_id": identifier, "product": payload["product"], "coverage": coverage}


def _capture_sample_work(store, configuration, source, fetcher):
    capacity = store.ensure_capacity(source.expected_length + MAX_PRODUCT)
    if isinstance(capacity, Err):
        return capacity
    baseline = current_manifest(store, "snapshot")
    if isinstance(baseline, Err):
        return baseline
    acquired = _acquire(store, source, baseline.value, fetcher)
    if isinstance(acquired, Err):
        return acquired
    descriptor, raw = acquired.value
    projected = project_sample(raw, _blob(descriptor["blob"]), configuration)
    if isinstance(projected, Err):
        return projected
    encoded = canonical_bytes(projected.value)
    if len(encoded) > MAX_PRODUCT:
        return Err("Candidate product exceeds 64 MiB")
    product = store.put(encoded)
    if isinstance(product, Err):
        return product
    producer = producer_identity()
    payload = {
        "schema_version": "cap.cold-snapshot/v1",
        "captured_at": _utc(),
        "source": descriptor,
        "configuration": projected.value["configuration"],
        "product": product.value.to_dict(),
        "producer": producer,
    }
    prior = baseline.value
    if prior and all(
        prior.get(key) == payload[key] for key in ("source", "configuration", "product", "producer")
    ):
        identifier = hashlib.sha256(canonical_bytes(prior)).hexdigest()
        return Ok(_snapshot_summary(identifier, prior, projected.value["coverage"]))
    accepted = store.accept("snapshot", payload, [_blob(descriptor["blob"]), product.value])
    if isinstance(accepted, Err):
        return accepted
    return Ok(_snapshot_summary(accepted.value, payload, projected.value["coverage"]))


def capture_sample(
    store: Store, configuration: dict, *, source: SourceSpec = DEFAULT_SOURCE, fetcher=fetch_bytes
) -> Result[dict]:
    initialized = store.init()
    if isinstance(initialized, Err):
        return initialized
    return store.locked(lambda: _capture_sample_work(store, configuration, source, fetcher))


def replay_snapshot(store: Store, payload: dict) -> Result[None]:
    try:
        if payload.get("schema_version") != "cap.cold-snapshot/v1":
            return Err("Unexpected historical snapshot contract")
        if payload["producer"]["recipe_sha256"] != recipe_digest():
            return Err("Recipe differs; use the archived producer revision for exact replay")
        spec = _source_spec(payload["source"])
        source = _blob(payload["source"]["blob"])
        raw = store.read(source, MAX_SOURCE)
        if isinstance(raw, Err):
            return raw
        _check_source(raw.value, spec)
        projected = project_sample(raw.value, source, payload["configuration"])
        if isinstance(projected, Err):
            return projected
        actual = store.read(_blob(payload["product"]), MAX_PRODUCT)
        if isinstance(actual, Err):
            return actual
        if canonical_bytes(projected.value) != actual.value:
            return Err("Offline candidate replay differs from recorded bytes")
        return Ok(None)
    except Exception as error:
        return Err(str(error))


def rebuild_snapshot(store: Store, snapshot_id: str | None = None) -> Result[dict]:
    loaded = store.load("snapshot", snapshot_id)
    if isinstance(loaded, Err):
        return loaded
    replayed = replay_snapshot(store, loaded.value)
    return (
        replayed
        if isinstance(replayed, Err)
        else Ok(
            {
                "snapshot_id": hashlib.sha256(canonical_bytes(loaded.value)).hexdigest(),
                "product": loaded.value["product"],
                "replay_verified": True,
            }
        )
    )


def capture_native_inventory(store: Store, *, fetcher=fetch_bytes) -> Result[dict]:
    initialized = store.init()
    if isinstance(initialized, Err):
        return initialized

    def work():
        capacity = store.ensure_capacity(24 * 1024**2)
        if isinstance(capacity, Err):
            return capacity
        captured = capture_inventory(fetcher=fetcher)
        if isinstance(captured, Err):
            return captured
        metadata_bytes = canonical_bytes(captured.value.manifest)
        if len(metadata_bytes) > 4 * 1024**2:
            return Err("Native inventory metadata exceeds the 4 MiB replay limit")
        references = []
        for raw in captured.value.raw_pages:
            reference = store.put(raw)
            if isinstance(reference, Err):
                return reference
            references.append(reference.value)
        metadata = store.put(metadata_bytes)
        if isinstance(metadata, Err):
            return metadata
        payload = {
            "schema_version": "cap.native-inventory-snapshot/v1",
            "observed_at": captured.value.manifest["observedAt"],
            "raw_pages": [ref.to_dict() for ref in references],
            "metadata": metadata.value.to_dict(),
            "producer": {
                "revision": producer_identity()["revision"],
                "recipe_sha256": recipe_digest(files=INVENTORY_RECIPE_FILES),
                "python_version": producer_identity()["python_version"],
            },
        }
        accepted = store.accept("inventory", payload, references + [metadata.value])
        return (
            accepted
            if isinstance(accepted, Err)
            else Ok({"inventory_id": accepted.value, "inventory": captured.value.manifest})
        )

    return store.locked(work)


def replay_inventory(store: Store, payload: dict) -> Result[None]:
    try:
        if payload.get("schema_version") != "cap.native-inventory-snapshot/v1":
            return Err("Unexpected native inventory contract")
        if payload.get("producer", {}).get("recipe_sha256") != recipe_digest(
            files=INVENTORY_RECIPE_FILES
        ):
            return Err("Inventory recipe differs; use the archived producer revision")
        pages = []
        for ref in payload["raw_pages"]:
            raw = store.read(_blob(ref), 1024**2)
            if isinstance(raw, Err):
                return raw
            pages.append(raw.value)
        parsed = parse_inventory_pages(pages, payload["observed_at"])
        if isinstance(parsed, Err):
            return parsed
        retained = store.read(_blob(payload["metadata"]), 4 * 1024**2)
        if isinstance(retained, Err):
            return retained
        return (
            Ok(None)
            if canonical_bytes(parsed.value) == retained.value
            else Err("Offline native inventory replay differs")
        )
    except Exception as error:
        return Err(str(error))
