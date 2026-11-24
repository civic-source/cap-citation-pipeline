"""Transactional historical shard capture and exact offline candidate replay."""
import hashlib
import math
from importlib.metadata import version
from pathlib import Path
import platform
import re
import subprocess
import tempfile

from bulk_common import Blob, Err, Ok, canonical_bytes, load_json
from bulk_archive import regular_file
from bulk_producer import INVENTORY_RECIPE_FILES, recipe_digest
from bulk_store import Store
from joined_rules import configuration
from joined_source import APPROVED_SOURCE, SourceSpec, acquire_source, download_source, validate_descriptor
from joined_worker import project_file

ROOT = Path(__file__).resolve().parent
JOINED_RECIPE_FILES = ("bulk_common.py", "bulk_store.py", "bulk_atomic.py", "bulk_github_transport.py", "bulk_producer.py", "usc_citations.py", "joined_source.py",
                       "joined_rules.py", "joined_output.py", "joined_projection.py", "joined_worker.py", "joined_pipeline.py")
PRODUCER_FILES = (*JOINED_RECIPE_FILES, "multipart_archive.py", "multipart_manifest.py", "multipart_github.py",
                  "joined.py", "bulk_archive.py", "bulk_github.py", "requirements-joined.txt")
PREFLIGHT_BYTES = 10 * 1024**3
APPROVED_SCHEMA = "6409ac865a0ed9bf693c99eb38fd3b181f2e3ddb9377bc443c61370232ff0718"


def require(result):
    if isinstance(result, Err):
        raise ValueError(result.error)
    return result.value


def source_spec(value):
    spec = SourceSpec(value["revision"], value["path"], value["expected_length"], value["publisher_sha256"])
    validate_descriptor(value, spec)
    return spec


def producer_identity(schema):
    files = PRODUCER_FILES
    dirty = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--", *files], text=True, timeout=15)
    revision = None if dirty.strip() else subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, timeout=15).strip()
    return {"revision": revision, "recipe_sha256": recipe_digest(files=JOINED_RECIPE_FILES),
            "inventory_recipe_sha256": recipe_digest(files=INVENTORY_RECIPE_FILES),
            "python_version": platform.python_version(), "pyarrow_version": version("pyarrow"), "schema_sha256": schema}


def verify_joined_producer(payload):
    try:
        producer = payload["producer"]
        revision = producer["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Commit the exact joined producer before archival")

        def read(name):
            return subprocess.check_output(["git", "-C", str(ROOT), "show", revision + ":" + name], stderr=subprocess.PIPE, timeout=15)

        if recipe_digest(read, JOINED_RECIPE_FILES) != producer["recipe_sha256"]:
            raise ValueError("Recorded producer does not retain exact joined recipe")
        if recipe_digest(read, INVENTORY_RECIPE_FILES) != producer["inventory_recipe_sha256"]:
            raise ValueError("Recorded producer does not retain exact inventory recipe")
        return Ok(revision)
    except Exception as error:
        return Err(str(error))


def _check_product(product, source):
    if product["pyarrow_version"] != "23.0.1":
        raise ValueError("Unexpected joined reader version")
    if source_spec(source) == APPROVED_SOURCE:
        expected = product["coverage"]
        if expected["records_scanned"] != 250162 or expected["row_groups_scanned"] != 8 or product["schema_sha256"] != APPROVED_SCHEMA:
            raise ValueError("Pinned shard row counts or schema differ from qualified footer")


def _project(store, descriptor, settings, output):
    blob = Blob(**descriptor["blob"])
    require(store.verify(blob))
    capacity = require(store.capacity())
    result = require(project_file(store.path(blob), blob, settings, output,
                                  limits={"min_free_bytes": max(4 * 1024**3, capacity["minFreeBytes"])}))
    require(store.verify(blob))
    _check_product(result, descriptor)
    return result


def _references(payload):
    return [Blob(**payload["source"]["blob"]), *[Blob(**part["blob"]) for part in payload["product"]["partitions"]]]


def _summary(identifier, payload):
    return {"snapshot_id": identifier, "source": payload["source"]["blob"],
            "coverage": payload["product"]["coverage"], "partitions": len(payload["product"]["partitions"])}


def _previous(store):
    try:
        (store.root / "current-joined-snapshot.json").lstat()
    except FileNotFoundError:
        return None
    return require(store.load("joined-snapshot"))


def _resources(path):
    with regular_file(path, 8192) as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("Worker resource observation exceeds byte limit")
    value = load_json(raw)
    if not isinstance(value, dict) or set(value) != {"peak_rss_bytes", "elapsed_seconds"}:
        raise ValueError("Invalid worker resource observation")
    if type(value["peak_rss_bytes"]) is not int or value["peak_rss_bytes"] < 0:
        raise ValueError("Invalid worker peak memory observation")
    if type(value["elapsed_seconds"]) not in (int, float) or not math.isfinite(value["elapsed_seconds"]) or value["elapsed_seconds"] < 0:
        raise ValueError("Invalid worker elapsed observation")
    return value


def _capture(store, settings, source, fetcher):
    require(store.ensure_capacity(PREFLIGHT_BYTES))
    previous = _previous(store)
    descriptor = require(acquire_source(store, source=source, fetcher=fetcher))
    with tempfile.TemporaryDirectory(prefix="joined-", dir=store.staging) as temporary:
        output = Path(temporary) / "projection"
        product = _project(store, descriptor, settings, output)
        for part in product["partitions"]:
            require(store.adopt(output / part["blob"]["sha256"], Blob(**part["blob"])))
        observations = _resources(output / "resources.json")
    payload = {"schema_version": "cap.joined-snapshot/v1", "captured_at": descriptor["acquired_at"],
               "source": descriptor, "configuration": settings, "product": product,
               "producer": producer_identity(product["schema_sha256"])}
    if previous is not None and previous == payload:
        return Ok({**_summary(hashlib.sha256(canonical_bytes(previous)).hexdigest(), previous), "resources": observations})
    identifier = require(store.accept("joined-snapshot", payload, _references(payload)))
    return Ok({**_summary(identifier, payload), "resources": observations})


def capture_joined(store, configuration_value, *, source=APPROVED_SOURCE, fetcher=download_source):
    try:
        settings, _ = configuration(configuration_value)
        require(store.init())
        return store.locked(lambda: _capture(store, settings, source, fetcher))
    except Exception as error:
        return Err(str(error))


def replay_joined_snapshot(store: Store, payload: dict):
    try:
        if payload.get("schema_version") != "cap.joined-snapshot/v1":
            raise ValueError("Unexpected joined snapshot schema")
        settings, _ = configuration(payload["configuration"])
        if settings != payload["configuration"]:
            raise ValueError("Noncanonical joined configuration")
        producer = payload["producer"]
        if producer["recipe_sha256"] != recipe_digest(files=JOINED_RECIPE_FILES):
            raise ValueError("Joined recipe differs; use retained producer")
        if producer["python_version"] != platform.python_version() or producer["pyarrow_version"] != version("pyarrow"):
            raise ValueError("Joined reader runtime differs from retained producer")
        descriptor = payload["source"]
        source_spec(descriptor)
        require(store.ensure_capacity(2 * 1024**3))
        with tempfile.TemporaryDirectory(prefix="joined-replay-", dir=store.staging) as temporary:
            output = Path(temporary) / "projection"
            product = _project(store, descriptor, settings, output)
            if product != payload["product"] or product["schema_sha256"] != producer["schema_sha256"]:
                raise ValueError("Offline joined projection differs from retained product")
            for part in product["partitions"]:
                reference = Blob(**part["blob"])
                require(store.verify(reference))
                if Blob.of((output / reference.sha256).read_bytes()) != reference:
                    raise ValueError("Replayed partition identity mismatch")
        return Ok(None)
    except Exception as error:
        return Err(str(error))


def rebuild_joined(store, snapshot_id=None):
    try:
        payload = require(store.load("joined-snapshot", snapshot_id))
        require(replay_joined_snapshot(store, payload))
        return Ok(_summary(hashlib.sha256(canonical_bytes(payload)).hexdigest(), payload))
    except Exception as error:
        return Err(str(error))
