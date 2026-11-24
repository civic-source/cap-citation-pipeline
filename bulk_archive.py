"""Deterministic, bounded archives of retained precedent inputs and products."""
from contextlib import contextmanager
import gzip
import hashlib
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
from uuid import uuid4

from bulk_common import Blob, Err, Ok, Result, canonical_bytes, load_json
from bulk_store import Store

MAX_ARCHIVE = 256 * 1024**2
MAX_MANIFEST = 4 * 1024**2
CHUNK = 65536


def references(value: object) -> set[Blob]:
    found = set()
    if isinstance(value, dict):
        if {"sha256", "byte_length"}.issubset(value):
            found.add(Blob(value["sha256"], value["byte_length"]))
        for child in value.values():
            found.update(references(child))
    elif isinstance(value, list):
        for child in value:
            found.update(references(child))
    sizes = {}
    for ref in found:
        if ref.sha256 in sizes and sizes[ref.sha256] != ref.byte_length:
            raise ValueError("Conflicting archived artifact lengths")
        sizes[ref.sha256] = ref.byte_length
    return found


@contextmanager
def regular_file(path: Path, limit=MAX_ARCHIVE):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Archive input must be a bounded regular file with one link")
        yield source
        current = os.fstat(source.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
            raise ValueError("Archive input changed during verification")


def archive_digest(path: Path) -> Result[Blob]:
    try:
        digest, size = hashlib.sha256(), 0
        with regular_file(path) as source:
            while chunk := source.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
        return Ok(Blob(digest.hexdigest(), size))
    except Exception as error:
        return Err(str(error))


def _entry(payload):
    return {"id": hashlib.sha256(canonical_bytes(payload)).hexdigest(), "payload": payload}


def _bundle(store, snapshot_id):
    snapshot = store.load("snapshot", snapshot_id)
    if isinstance(snapshot, Err):
        raise ValueError(snapshot.error)
    inventory = None
    try:
        (store.root / "current-inventory.json").lstat()
    except FileNotFoundError:
        pass
    else:
        loaded = store.load("inventory")
        if isinstance(loaded, Err):
            raise ValueError(loaded.error)
        inventory = _entry(loaded.value)
    found = references(snapshot.value) | references(inventory)
    result = {"schema_version": "cap.precedent-archive/v1", "snapshot": _entry(snapshot.value), "inventory": inventory,
              "objects": [ref.to_dict() for ref in sorted(found, key=lambda item: item.sha256)]}
    _validate_bundle(result)
    return result


def _validate_entry(value):
    if not isinstance(value, dict) or set(value) != {"id", "payload"} or not isinstance(value["payload"], dict):
        raise ValueError("Invalid archived manifest entry")
    if value["id"] != hashlib.sha256(canonical_bytes(value["payload"])).hexdigest():
        raise ValueError("Archived manifest identity mismatch")


def _compatible_inventory_recipe(value):
    if value["inventory"] is None:
        return
    case_producer = value["snapshot"]["payload"].get("producer")
    inventory_producer = value["inventory"]["payload"].get("producer")
    if not isinstance(case_producer, dict) or not isinstance(inventory_producer, dict):
        raise ValueError("Archive inventory recipe producer metadata is missing")
    expected = case_producer.get("inventory_recipe_sha256")
    actual = inventory_producer.get("recipe_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or actual != expected:
        raise ValueError("Archive inventory recipe differs from the case producer; capture a compatible new generation")


def _validate_bundle(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "snapshot", "inventory", "objects"} or value["schema_version"] != "cap.precedent-archive/v1":
        raise ValueError("Invalid precedent archive manifest")
    _validate_entry(value["snapshot"])
    if value["inventory"] is not None:
        _validate_entry(value["inventory"])
    _compatible_inventory_recipe(value)
    if not isinstance(value["objects"], list) or len(value["objects"]) > 4096:
        raise ValueError("Archive object inventory exceeds its bound")
    blobs = []
    for item in value["objects"]:
        if not isinstance(item, dict) or set(item) != {"sha256", "byte_length"}:
            raise ValueError("Invalid archived object reference")
        blobs.append(Blob(**item))
    if len({ref.sha256 for ref in blobs}) != len(blobs) or blobs != sorted(blobs, key=lambda ref: ref.sha256):
        raise ValueError("Duplicate or unsorted archived objects")
    if set(blobs) != references(value["snapshot"]["payload"]) | references(value["inventory"]):
        raise ValueError("Archive reference closure mismatch")
    if sum(ref.byte_length for ref in blobs) > MAX_ARCHIVE:
        raise ValueError("Archive exceeds 256 MiB unpacked pilot budget")
    return blobs


def _exact(source, length):
    chunks, remaining = [], length
    while remaining:
        data = source.read(min(CHUNK, remaining))
        if not data:
            raise ValueError("Truncated archive member")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _header(raw):
    if raw[257:265] != b"ustar\x0000" or raw[156:157] not in {b"0", b"\0"} or any(raw[157:257]) or any(raw[345:500]):
        raise ValueError("Linked, extended or non-USTAR archive member")
    checksum = int(raw[148:156].strip(b"\0 ") or b"0", 8)
    if sum(raw[:148]) + 8 * 32 + sum(raw[156:]) != checksum:
        raise ValueError("Invalid archive header checksum")
    encoded_size = raw[124:136].strip(b"\0 ") or b"0"
    if not re.fullmatch(rb"[0-7]{1,11}", encoded_size):
        raise ValueError("Invalid archive member length")
    return raw[:100].split(b"\0", 1)[0].decode("ascii"), int(encoded_size, 8)


def _copy_member(source, size, expected, destination=None):
    digest, remaining = hashlib.sha256(), size
    output = open(destination, "xb") if destination is not None else None
    try:
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ValueError("Truncated archive object")
            remaining -= len(chunk)
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Archived artifact hash mismatch")
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
    finally:
        if output is not None:
            output.close()


def _finish_archive(source):
    if any(_exact(source, 512)):
        raise ValueError("Malformed archive terminator")
    padding = 0
    while chunk := source.read(CHUNK):
        padding += len(chunk)
        if any(chunk) or padding > 10240:
            raise ValueError("Unexpected bytes after archive terminator")


def _scan(path, destination=None):
    manifest, expected, seen = None, {}, set()
    with regular_file(path) as raw, gzip.GzipFile(fileobj=raw, mode="rb") as source:
        while True:
            header = _exact(source, 512)
            if not any(header):
                _finish_archive(source)
                break
            name, size = _header(header)
            if name in seen:
                raise ValueError("Duplicate archive member")
            seen.add(name)
            if manifest is None:
                if name != "archive.json" or size > MAX_MANIFEST:
                    raise ValueError("First archive member must be a bounded manifest")
                content = _exact(source, size)
                manifest = load_json(content)
                blobs = _validate_bundle(manifest)
                if canonical_bytes(manifest) != content:
                    raise ValueError("Archive manifest is not canonical")
                expected = {"objects/" + ref.sha256: ref for ref in blobs}
            else:
                if name not in expected or size != expected[name].byte_length:
                    raise ValueError("Unexpected archive member or byte length")
                target = destination / expected[name].sha256 if destination is not None else None
                _copy_member(source, size, expected[name].sha256, target)
            if any(_exact(source, -size % 512)):
                raise ValueError("Nonzero archive member padding")
    if manifest is None or seen != {"archive.json", *expected}:
        raise ValueError("Incomplete precedent archive")
    return manifest


def inspect_archive(path: Path) -> Result[dict]:
    try:
        return Ok(_scan(path))
    except Exception as error:
        return Err(str(error))


def _write_archive(store, bundle, path):
    manifest = canonical_bytes(bundle)
    if len(manifest) > MAX_MANIFEST:
        raise ValueError("Archive manifest exceeds its byte limit")
    with open(path, "xb") as output, gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
            for name, size in [("archive.json", len(manifest)), *(("objects/" + item["sha256"], item["byte_length"]) for item in bundle["objects"])]:
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = size, 0o600
                if name == "archive.json":
                    import io
                    archive.addfile(entry, io.BytesIO(manifest))
                else:
                    with regular_file(store.path(Blob(name.removeprefix("objects/"), size))) as source:
                        archive.addfile(entry, source)
        zipped.close()
        output.flush()
        os.fsync(output.fileno())


def _output_space(directory, additional, reserve):
    filesystem = os.statvfs(directory)
    if filesystem.f_bavail * filesystem.f_frsize - reserve < additional:
        raise ValueError("Insufficient archive output filesystem capacity")


def _publish(staged, output, reference, reserve):
    directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".precedent-" + uuid4().hex
    try:
        _output_space(output.parent, reference.byte_length, reserve)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as target, regular_file(staged) as source:
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > reference.byte_length:
                    raise ValueError("Archive changed while publishing")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if Blob(digest.hexdigest(), size) != reference:
            raise ValueError("Archive publication hash mismatch")
        os.link(temporary, output.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def export_snapshot(store: Store, snapshot_id: str, output: Path) -> Result[Blob]:
    try:
        bundle = _bundle(store, snapshot_id)
        budget = sum(item["byte_length"] for item in bundle["objects"]) + MAX_MANIFEST + 1024**2
        capacity, status = store.ensure_capacity(2 * budget), store.capacity()
        if isinstance(capacity, Err) or isinstance(status, Err):
            return capacity if isinstance(capacity, Err) else status
        destination = Path(os.path.abspath(output))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _output_space(destination.parent, budget, status.value["minFreeBytes"])
        with tempfile.TemporaryDirectory(prefix="export-", dir=store.staging) as temporary:
            path = Path(temporary) / "archive.tar.gz"
            _write_archive(store, bundle, path)
            _scan(path)
            result = archive_digest(path)
            if isinstance(result, Err):
                return result
            _publish(path, destination, result.value, status.value["minFreeBytes"])
            return result
    except Exception as error:
        return Err(str(error))


def import_snapshot(store: Store, archive_path: Path, *, expected_snapshot_id: str | None = None, expected_producer_revision: str | None = None) -> Result[str]:
    try:
        bundle = _scan(archive_path)
        if expected_snapshot_id is not None and bundle["snapshot"]["id"] != expected_snapshot_id:
            return Err("Archive snapshot differs from authenticated receipt")
        if expected_producer_revision is not None and bundle["snapshot"]["payload"].get("producer", {}).get("revision") != expected_producer_revision:
            return Err("Archive producer differs from authenticated receipt")
        blobs = _validate_bundle(bundle)
        capacity = store.ensure_capacity(sum(ref.byte_length for ref in blobs) + MAX_MANIFEST)
        if isinstance(capacity, Err):
            return capacity
        with tempfile.TemporaryDirectory(prefix="import-", dir=store.staging) as temporary:
            if _scan(archive_path, Path(temporary)) != bundle:
                raise ValueError("Archive changed during import")
            for reference in blobs:
                adopted = store.adopt(Path(temporary) / reference.sha256, reference)
                if isinstance(adopted, Err):
                    return adopted
        return _replay_and_accept(store, bundle)
    except Exception as error:
        return Err(str(error))


def _replay_and_accept(store, bundle):
    from bulk_pipeline import replay_inventory, replay_snapshot
    for kind in ("snapshot", "inventory"):
        entry = bundle[kind]
        if entry is not None:
            result = (replay_snapshot if kind == "snapshot" else replay_inventory)(store, entry["payload"])
            if isinstance(result, Err):
                return result
    if bundle["inventory"] is not None:
        payload = bundle["inventory"]["payload"]
        accepted = store.accept("inventory", payload, list(references(payload)))
        if isinstance(accepted, Err):
            return accepted
    payload = bundle["snapshot"]["payload"]
    return store.accept("snapshot", payload, list(references(payload)))
