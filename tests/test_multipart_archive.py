import copy
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from bulk_common import Blob, Err, Ok, canonical_bytes
from bulk_store import Store
from multipart_archive import export_joined, import_joined, inspect_joined
from multipart_manifest import validate_index
from joined_source import SourceSpec
import multipart_archive


def accepted(result):
    assert isinstance(result, Ok), result
    return result.value


def snapshot(store, source=b"original parquet bytes", evidence=b'{"candidate":true}\n'):
    accepted(store.init())
    raw = accepted(store.put(source))
    output = accepted(store.put(evidence))
    descriptor = SourceSpec("a" * 40, "part-00000-00000000-0000-0000-0000-000000000000-c000.gz.parquet",
                            len(source), raw.sha256).descriptor()
    payload = {
        "schema_version": "cap.joined-snapshot/v1",
        "source": {**descriptor, "blob": raw.to_dict(), "acquired_at": "2026-11-24T00:00:00+00:00", "headers": {}},
        "product": {"partitions": [{"blob": output.to_dict()}]},
        "producer": {"revision": None, "inventory_recipe_sha256": "f" * 64},
    }
    identifier = accepted(store.accept("joined-snapshot", payload, [raw, output]))
    return identifier, payload


def replay(store, payload):
    for ref in [payload["source"]["blob"], payload["product"]["partitions"][0]["blob"]]:
        result = store.verify(Blob(**ref))
        if isinstance(result, Err):
            return result
    return Ok(None)


def test_multiple_raw_parts_reconstruct_full_objects_deterministically(tmp_path):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, payload = snapshot(original)
    first = accepted(export_joined(original, identifier, tmp_path / "export-a", part_size=8))
    second = accepted(export_joined(original, identifier, tmp_path / "export-b", part_size=8))
    assert first["index"] == second["index"]
    assert len(first["parts"]) >= 3
    index = accepted(inspect_joined(Path(first["index_path"])))
    assert index["snapshot"]["id"] == identifier
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    accepted(restored.init())
    assert accepted(import_joined(restored, Path(first["index_path"]), replayer=replay)) == identifier
    assert accepted(restored.read(Blob(**payload["source"]["blob"]))) == b"original parquet bytes"


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "extra", "symlink"])
def test_bad_transport_preserves_prior_pointer(tmp_path, mutation):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    directory = Path(exported["index_path"]).parent
    part = next(directory.glob("part-*.bin"))
    if mutation == "missing":
        part.unlink()
    elif mutation == "corrupt":
        part.write_bytes(b"x" * part.stat().st_size)
    elif mutation == "extra":
        (directory / "unlisted.bin").write_bytes(b"extra")
    else:
        moved = tmp_path / "moved"
        part.rename(moved)
        part.symlink_to(moved)
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    prior, _ = snapshot(restored, b"prior source bytes", b"prior evidence")
    assert isinstance(import_joined(restored, Path(exported["index_path"]), replayer=replay), Err)
    assert hashlib.sha256(canonical_bytes(accepted(restored.load("joined-snapshot")))).hexdigest() == prior


def test_failed_domain_replay_and_expected_identity_preserve_selection(tmp_path):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    prior, _ = snapshot(restored, b"prior source bytes", b"prior evidence")
    failed = import_joined(restored, Path(exported["index_path"]), replayer=lambda *_: Err("bad recipe"))
    assert isinstance(failed, Err) and "bad recipe" in failed.error
    mismatch = import_joined(restored, Path(exported["index_path"]), expected_snapshot_id="0" * 64, replayer=replay)
    assert isinstance(mismatch, Err)
    assert hashlib.sha256(canonical_bytes(accepted(restored.load("joined-snapshot")))).hexdigest() == prior


def test_invalid_offsets_lengths_and_closure_are_rejected(tmp_path):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    index = accepted(inspect_joined(Path(exported["index_path"])))
    for mutation in ("offset", "length", "closure", "duplicate", "zero"):
        changed = copy.deepcopy(index)
        if mutation == "offset":
            changed["objects"][0]["parts"][0]["offset"] = 1
        elif mutation == "length":
            changed["objects"][0]["parts"][0]["blob"]["byte_length"] += 1
        elif mutation == "closure":
            changed["objects"].pop()
        elif mutation == "duplicate":
            changed["objects"].append(changed["objects"][0])
        else:
            changed["objects"][0]["parts"][0]["blob"]["byte_length"] = 0
        with pytest.raises(ValueError):
            validate_index(changed)


def test_existing_export_path_and_insufficient_capacity_are_refused(tmp_path):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    destination = tmp_path / "export"
    accepted(export_joined(original, identifier, destination, part_size=8))
    assert isinstance(export_joined(original, identifier, destination, part_size=8), Err)
    original._free_provider = lambda: 0
    assert isinstance(export_joined(original, identifier, tmp_path / "no-space", part_size=8), Err)


def test_zero_byte_object_has_no_parts_and_verified_empty_digest(tmp_path):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original, evidence=b"")
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    index = accepted(inspect_joined(Path(exported["index_path"])))
    empty = next(value for value in index["objects"] if value["byte_length"] == 0)
    assert empty["parts"] == [] and empty["sha256"] == hashlib.sha256(b"").hexdigest()


def test_exclusive_reconstruction_collision_never_deletes_existing_stage(tmp_path, monkeypatch):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    accepted(restored.init())
    protected = restored.staging / "joined-object-fixed"
    protected.write_bytes(b"existing staging bytes")
    monkeypatch.setattr(multipart_archive, "uuid4", lambda: type("Fixed", (), {"hex": "fixed"})())
    result = import_joined(restored, Path(exported["index_path"]), replayer=replay)
    assert isinstance(result, Err)
    assert protected.read_bytes() == b"existing staging bytes"


def test_part_count_refused_before_writing_source_extents(tmp_path, monkeypatch):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original, b"a" * 65, b"")
    monkeypatch.setattr(multipart_archive, "_split_object", lambda *_: pytest.fail("part budget was not preflighted"))
    result = export_joined(original, identifier, tmp_path / "export", part_size=1)
    assert isinstance(result, Err) and "extent" in result.error


def test_export_checks_free_reserve_during_streaming(tmp_path, monkeypatch):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    checks = []
    def checked_space(*args):
        checks.append(args)
        if len(checks) > 1:
            raise ValueError("capacity changed")
    monkeypatch.setattr(multipart_archive, "_space", checked_space)
    result = export_joined(original, identifier, tmp_path / "export", part_size=8)
    assert isinstance(result, Err) and "capacity changed" in result.error
    assert len(checks) > 1


def test_export_files_are_private_under_standard_umask(tmp_path):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    previous = os.umask(0o022)
    try:
        exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    finally:
        os.umask(previous)
    assert all(path.stat().st_mode & 0o077 == 0 for path in Path(exported["index_path"]).parent.iterdir())


@pytest.mark.parametrize("fail_selection", [False, True])
def test_optional_inventory_is_retained_without_changing_inventory_selection(tmp_path, monkeypatch, fail_selection):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    metadata = accepted(original.put(b"context"))
    context = {"schema_version": "cap.native-inventory-snapshot/v1", "metadata": metadata.to_dict(),
               "producer": {"recipe_sha256": "f" * 64}}
    context_id = accepted(original.accept("inventory", context, [metadata]))
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8, inventory_id=context_id))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    prior, _ = snapshot(restored, b"prior source bytes", b"prior evidence")
    old_blob = accepted(restored.put(b"old context"))
    old_context = {"schema_version": "cap.native-inventory-snapshot/v1", "metadata": old_blob.to_dict(),
                   "producer": {"recipe_sha256": "f" * 64}}
    accepted(restored.accept("inventory", old_context, [old_blob]))
    import bulk_pipeline
    monkeypatch.setattr(bulk_pipeline, "replay_inventory", lambda *_: Ok(None))
    if fail_selection:
        accept = restored.accept
        monkeypatch.setattr(restored, "accept", lambda kind, *args: Err("final selection failed")
                            if kind == "joined-snapshot" else accept(kind, *args))
    result = import_joined(restored, Path(exported["index_path"]), replayer=replay)
    assert isinstance(result, Err if fail_selection else Ok)
    assert accepted(restored.load("inventory")) == old_context
    assert accepted(restored.load("inventory", context_id)) == context
    expected = prior if fail_selection else identifier
    assert hashlib.sha256(canonical_bytes(accepted(restored.load("joined-snapshot")))).hexdigest() == expected


def test_concurrently_growing_part_is_refused_after_bounded_read(tmp_path, monkeypatch):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    exported = accepted(export_joined(original, identifier, tmp_path / "export", part_size=8))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    prior, _ = snapshot(restored, b"prior source bytes", b"prior evidence")
    regular = multipart_archive.regular_file
    reads = []
    @contextmanager
    def growing(path, limit):
        with regular(path, limit) as source:
            if path.name.startswith("part-"):
                class Growing:
                    def read(self, size):
                        reads.append(size)
                        if len(reads) > 4:
                            raise RuntimeError("bounded test sentinel")
                        with open(path, "ab") as append:
                            append.write(b"abcdefgh")
                        return source.read(size)
                yield Growing()
            else:
                yield source
    monkeypatch.setattr(multipart_archive, "regular_file", growing)
    result = import_joined(restored, Path(exported["index_path"]), replayer=replay)
    assert isinstance(result, Err)
    assert len(reads) == 1
    assert hashlib.sha256(canonical_bytes(accepted(restored.load("joined-snapshot")))).hexdigest() == prior


def test_staged_output_is_read_back_before_local_publication(tmp_path, monkeypatch):
    original = Store(tmp_path / "original", min_free_bytes=0)
    identifier, _ = snapshot(original)
    copy_extent = multipart_archive._copy_extent
    def corrupt_output(source, path, length, whole, guard):
        result = copy_extent(source, path, length, whole, guard)
        path.write_bytes(b"x" * length)
        return result
    monkeypatch.setattr(multipart_archive, "_copy_extent", corrupt_output)
    destination = tmp_path / "export"
    result = export_joined(original, identifier, destination, part_size=8)
    assert isinstance(result, Err)
    assert not destination.exists()
