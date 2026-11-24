"""Verified raw-part export and replay-before-selection reconstruction."""

import hashlib
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from bulk_archive import references, regular_file
from bulk_common import Blob, Err, Ok, canonical_bytes, load_json
from multipart_manifest import (
    INDEX_BYTES, MAX_PARTS, PART_BYTES, TOTAL_BYTES, entry, part_inventory, part_name, validate_index,
)

CHUNK = 65536


def inspect_joined(index_path):
    try:
        with regular_file(Path(index_path), INDEX_BYTES) as source:
            raw = source.read(INDEX_BYTES + 1)
        index = load_json(raw)
        if canonical_bytes(index) != raw:
            raise ValueError("Multipart index is not canonical")
        validate_index(index)
        return Ok(index)
    except Exception as error:
        return Err(str(error))


def _checked(result):
    if isinstance(result, Err):
        raise ValueError(result.error)
    return result.value


def _hash_file(path, reference):
    digest, size = hashlib.sha256(), 0
    with regular_file(path, reference.byte_length) as source:
        while chunk := source.read(min(CHUNK, reference.byte_length - size + 1)):
            size += len(chunk)
            if size > reference.byte_length:
                raise ValueError("Multipart file exceeds its expected byte length")
            digest.update(chunk)
    if Blob(digest.hexdigest(), size) != reference:
        raise ValueError("Multipart file hash or length mismatch")


def _space(directory, required, reserve):
    info = os.statvfs(directory)
    if info.f_bavail * info.f_frsize - reserve < required:
        raise ValueError("Insufficient multipart filesystem space and reserve")


def _copy_extent(source, path, length, whole, guard):
    digest, remaining = hashlib.sha256(), length
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ValueError("Truncated multipart source object")
            guard(len(chunk))
            output.write(chunk)
            whole.update(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    return Blob(digest.hexdigest(), length)


def _split_object(store, reference, directory, part_size):
    _checked(store.verify(reference))
    parts, offset, whole = [], 0, hashlib.sha256()
    reserve = _checked(store.capacity())["minFreeBytes"]
    def guard(additional):
        _checked(store.ensure_capacity(additional))
        _space(directory, additional, reserve)
    with regular_file(store.path(reference), TOTAL_BYTES) as source:
        while offset < reference.byte_length:
            staged = directory / (".part-" + uuid4().hex)
            ref = _copy_extent(source, staged, min(part_size, reference.byte_length - offset), whole, guard)
            final = directory / part_name(ref)
            try:
                os.link(staged, final, follow_symlinks=False)
            except FileExistsError:
                _hash_file(final, ref)
            staged.unlink()
            parts.append({"offset": offset, "blob": ref.to_dict()})
            offset += ref.byte_length
        if source.read(1) or Blob(whole.hexdigest(), offset) != reference:
            raise ValueError("Multipart source changed or has a wrong digest")
    return {**reference.to_dict(), "parts": parts}


def _index(store, snapshot_id, inventory_id):
    snapshot = _checked(store.load("joined-snapshot", snapshot_id))
    inventory = None if inventory_id is None else entry("inventory", _checked(store.load("inventory", inventory_id)))
    found = references(snapshot) | references(inventory)
    if sum(ref.byte_length for ref in found) > TOTAL_BYTES:
        raise ValueError("Multipart closure exceeds its byte budget")
    return {"schema_version": "cap.multipart-archive/v1", "snapshot": entry("joined-snapshot", snapshot),
            "inventory": inventory, "objects": []}, sorted(found, key=lambda ref: ref.sha256)


def _publish_directory(staged, destination):
    destination.mkdir(mode=0o700)
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identity, published = os.fstat(descriptor), []
    try:
        names = sorted(staged.iterdir(), key=lambda path: path.name.startswith("joined-index-"))
        for path in names:
            os.link(path, path.name, dst_dir_fd=descriptor, follow_symlinks=False)
            published.append(path.name)
            path.unlink()
        os.fsync(descriptor)
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except Exception:
        for name in published:
            os.unlink(name, dir_fd=descriptor)
        current = destination.lstat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            destination.rmdir()
        raise
    finally:
        os.close(descriptor)


def export_joined(store, snapshot_id, output_directory, *, part_size=PART_BYTES, inventory_id=None):
    try:
        if type(part_size) is not int or not 0 < part_size <= PART_BYTES:
            raise ValueError("Invalid multipart export part size")
        index, objects = _index(store, snapshot_id, inventory_id)
        if sum((ref.byte_length + part_size - 1) // part_size for ref in objects) > MAX_PARTS:
            raise ValueError("Multipart archive exceeds its extent budget")
        budget = sum(ref.byte_length for ref in objects) + INDEX_BYTES
        _checked(store.ensure_capacity(budget))
        destination = Path(os.path.abspath(output_directory))
        if destination.exists() or destination.is_symlink():
            raise ValueError("Multipart export destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        _space(destination.parent, budget, _checked(store.capacity())["minFreeBytes"])
        with tempfile.TemporaryDirectory(prefix=".joined-export-", dir=destination.parent) as temporary:
            directory = Path(temporary)
            index["part_size"] = part_size
            index["objects"] = [_split_object(store, ref, directory, part_size) for ref in objects]
            validate_index(index)
            raw = canonical_bytes(index)
            reference = Blob.of(raw)
            name = f"joined-index-{reference.sha256}.json"
            descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _hash_file(directory / name, reference)
            _transport_files(directory / name, index)
            _publish_directory(directory, destination)
        return Ok({"snapshot_id": snapshot_id, "index": reference.to_dict(),
                   "index_path": str(destination / name),
                   "parts": [ref.to_dict() for ref in part_inventory(index)]})
    except Exception as error:
        return Err(str(error))


def _transport_files(index_path, index):
    directory = index_path.parent
    expected = {index_path.name, *(part_name(ref) for ref in part_inventory(index))}
    if {path.name for path in directory.iterdir()} != expected:
        raise ValueError("Missing or unexpected multipart transport files")
    for ref in part_inventory(index):
        _hash_file(directory / part_name(ref), ref)


def _remove_owned(path, identity):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
        raise ValueError("Multipart stage identity changed; cleanup refused")
    path.unlink()


def _restore_object(store, value, directory):
    reference = Blob(value["sha256"], value["byte_length"])
    staged = store.staging / ("joined-object-" + uuid4().hex)
    whole, count = hashlib.sha256(), 0
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for extent in value["parts"]:
                ref = Blob(**extent["blob"])
                digest, length = hashlib.sha256(), 0
                with regular_file(directory / part_name(ref), ref.byte_length) as source:
                    while chunk := source.read(CHUNK):
                        count += len(chunk)
                        length += len(chunk)
                        if length > ref.byte_length or count > reference.byte_length:
                            raise ValueError("Multipart extent changed during reconstruction")
                        _checked(store.ensure_capacity(len(chunk)))
                        output.write(chunk)
                        whole.update(chunk)
                        digest.update(chunk)
                if Blob(digest.hexdigest(), length) != ref:
                    raise ValueError("Multipart extent changed during reconstruction")
            output.flush()
            os.fsync(output.fileno())
        if Blob(whole.hexdigest(), count) != reference:
            raise ValueError("Reconstructed multipart object identity mismatch")
        _checked(store.adopt(staged, reference))
    finally:
        _remove_owned(staged, identity)


def _domain_replay(store, index, replayer):
    if replayer is None:
        from joined_pipeline import replay_joined_snapshot
        replayer = replay_joined_snapshot
    inventory = index["inventory"]
    if inventory is not None:
        from bulk_pipeline import replay_inventory
        _checked(replay_inventory(store, inventory["payload"]))
    payload = index["snapshot"]["payload"]
    _checked(replayer(store, payload))
    _restore_acquisition(store, payload)
    if inventory is not None:
        payload = inventory["payload"]
        _checked(store.retain(payload, list(references(payload))))
    payload = index["snapshot"]["payload"]
    return store.accept("joined-snapshot", payload, list(references(payload)))


def _restore_acquisition(store, payload):
    from joined_pipeline import source_spec
    from joined_source import acquire_source
    source = payload["source"]
    _checked(acquire_source(store, source=source_spec(source), fetcher=lambda *_: Ok(source)))


def import_joined(store, index_path, *, expected_snapshot_id=None, expected_producer_revision=None, replayer=None):
    try:
        path = Path(index_path)
        index = _checked(inspect_joined(path))
        snapshot = index["snapshot"]
        if expected_snapshot_id is not None and snapshot["id"] != expected_snapshot_id:
            raise ValueError("Multipart snapshot differs from expected identity")
        producer = snapshot["payload"].get("producer", {}).get("revision")
        if expected_producer_revision is not None and producer != expected_producer_revision:
            raise ValueError("Multipart producer differs from expected identity")
        _transport_files(path, index)
        total = sum(ref.byte_length for ref in validate_index(index))
        _checked(store.ensure_capacity(total + INDEX_BYTES))
        for value in index["objects"]:
            _restore_object(store, value, path.parent)
        _checked(store.put(canonical_bytes(index)))
        return _domain_replay(store, index, replayer)
    except Exception as error:
        return Err(str(error))
