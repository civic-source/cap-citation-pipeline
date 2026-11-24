"""Archive transport preserves exact references and rejects unsafe containers."""
import gzip
import hashlib
import io
from pathlib import Path
import tarfile

import pytest

from bulk_archive import export_snapshot, import_snapshot
from bulk_common import Blob, Err, Ok, canonical_bytes
from bulk_store import Store


def value(result):
    assert isinstance(result, Ok), result
    return result.value


def fixture(tmp_path):
    from bulk_http import HttpArtifact
    from bulk_pipeline import SourceSpec, capture_sample
    store = Store(tmp_path / "cache", min_free_bytes=0)
    value(store.init())
    row = {"id": 1, "case_name": "Fixture", "date_filed": "2020-01-01", "date_filed_is_approximate": False,
           "court_type": "F", "court_jurisdiction": "USA, Federal", "court_full_name": "Court of Appeals for the Ninth Circuit",
           "court_short_name": "Ninth Circuit", "citations": ["100 F.4th 1"],
           "opinions": [{"opinion_id": 2, "opinion_text": "The charge cites 18 U.S.C. § 111(a).", "type": "020lead", "ocr": False}]}
    raw = canonical_bytes(row)
    source = SourceSpec("0" * 40, "2023-10-19", len(raw), hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest())
    captured = value(capture_sample(store, {"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": None},
                                   source=source, fetcher=lambda url, *_args, **_kwargs: Ok(HttpArtifact(url, 200, {}, raw))))
    payload = value(store.load("snapshot", captured["snapshot_id"]))
    payload["producer"]["revision"] = "0" * 40
    source_blob, product = Blob(**payload["source"]["blob"]), Blob(**payload["product"])
    snapshot_id = value(store.accept("snapshot", payload, [source_blob, product]))
    return store, snapshot_id, payload, source_blob


def test_exports_identical_complete_bundles_and_does_not_replace_files(tmp_path):
    store, snapshot_id, payload, raw = fixture(tmp_path)
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    assert value(export_snapshot(store, snapshot_id, first)) == value(export_snapshot(store, snapshot_id, second))
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames()[0] == "archive.json"
        assert archive.extractfile("objects/" + raw.sha256).read() == value(store.read(raw))
    before = first.read_bytes()
    assert isinstance(export_snapshot(store, snapshot_id, first), Err)
    assert first.read_bytes() == before


@pytest.mark.parametrize("mode", ["traversal", "duplicate", "link", "extra", "missing", "hash", "truncated"])
def test_rejects_altered_archives_without_changing_the_case_pointer(tmp_path, mode):
    store, snapshot_id, payload, raw = fixture(tmp_path)
    good, bad = tmp_path / "good.tar.gz", tmp_path / "bad.tar.gz"
    value(export_snapshot(store, snapshot_id, good))
    if mode == "truncated":
        bad.write_bytes(good.read_bytes()[:-8])
    else:
        with tarfile.open(good, "r:gz") as source, tarfile.open(bad, "w:gz", format=tarfile.USTAR_FORMAT) as target:
            members = source.getmembers()
            for index, entry in enumerate(members):
                if mode == "missing" and index == len(members) - 1:
                    continue
                content = source.extractfile(entry).read()
                if mode == "hash" and index == 1:
                    content = b"x" * len(content)
                target.addfile(entry, io.BytesIO(content))
            if mode in {"traversal", "duplicate", "link", "extra"}:
                name = {"traversal": "../escape", "duplicate": "archive.json", "link": "link", "extra": "unexpected"}[mode]
                entry = tarfile.TarInfo(name)
                if mode == "link":
                    entry.type, entry.linkname = tarfile.SYMTYPE, "../escape"
                target.addfile(entry, io.BytesIO())
    pointer = (store.root / "current-snapshot.json").read_bytes()
    assert isinstance(import_snapshot(store, bad), Err)
    assert (store.root / "current-snapshot.json").read_bytes() == pointer


def test_refuses_a_corrupt_existing_inventory_pointer_instead_of_dropping_it(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    (store.root / "current-inventory.json").write_bytes(b"corrupt")
    assert isinstance(export_snapshot(store, snapshot_id, tmp_path / "data.tar.gz"), Err)


def test_rejects_expansion_before_allocating_or_accepting(tmp_path):
    store, _, _, _ = fixture(tmp_path)
    path = tmp_path / "oversized.tar.gz"
    header = tarfile.TarInfo("archive.json")
    header.size = 4 * 1024**2 + 1
    with gzip.open(path, "wb") as output:
        output.write(header.tobuf(format=tarfile.USTAR_FORMAT))
        output.write(b"x" * 1024)
    assert isinstance(import_snapshot(store, path), Err)


def test_restores_after_complete_source_loss_and_replays_before_acceptance(tmp_path):
    store, snapshot_id, payload, _ = fixture(tmp_path)
    path = tmp_path / "recovery.tar.gz"
    value(export_snapshot(store, snapshot_id, path))
    import shutil
    shutil.rmtree(store.root)
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    value(restored.init())
    assert value(import_snapshot(restored, path)) == snapshot_id
    assert value(restored.load("snapshot")) == payload


def test_semantically_invalid_but_hash_consistent_products_cannot_replace_current(tmp_path):
    store, snapshot_id, payload, source = fixture(tmp_path)
    prior = (store.root / "current-snapshot.json").read_bytes()
    changed = value(store.put(b'{"not":"a replayed product"}\n'))
    other = {**payload, "product": changed.to_dict()}
    candidate = value(store.accept("snapshot", other, [source, changed]))
    bad = tmp_path / "invalid-product.tar.gz"
    value(export_snapshot(store, candidate, bad))
    value(store.accept("snapshot", payload, [source, Blob(**payload["product"])]))
    assert isinstance(import_snapshot(store, bad), Err)
    assert (store.root / "current-snapshot.json").read_bytes() == prior


def add_inventory(store):
    from bulk_http import HttpArtifact
    from bulk_pipeline import capture_native_inventory
    raw = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>com-courtlistener-storage</Name><Prefix>bulk-data/</Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated></ListBucketResult>'
    value(capture_native_inventory(store, fetcher=lambda url, *_args, **_kwargs: Ok(HttpArtifact(url, 200, {"content-type": "application/xml"}, raw))))
    return value(store.load("inventory"))


def test_includes_and_replays_native_inventory_before_accepting_pointers(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    inventory = add_inventory(store)
    path = tmp_path / "with-inventory.tar.gz"
    value(export_snapshot(store, snapshot_id, path))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    value(restored.init())
    assert value(import_snapshot(restored, path)) == snapshot_id
    assert value(restored.load("inventory")) == inventory


def test_checks_external_output_filesystem_and_cache_quota(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    store, snapshot_id, _, _ = fixture(tmp_path)
    directory = tmp_path / "output"
    directory.mkdir()
    original = os.statvfs
    monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(f_bavail=0, f_frsize=1) if Path(path) == directory else original(path))
    assert isinstance(export_snapshot(store, snapshot_id, directory / "archive.tar.gz"), Err)
    assert not (directory / "archive.tar.gz").exists()
    used = value(store.capacity())["cacheBytes"]
    limited = Store(store.root, min_free_bytes=0, max_cache_bytes=used + 1)
    assert isinstance(export_snapshot(limited, snapshot_id, tmp_path / "quota.tar.gz"), Err)


def test_authenticated_restore_identity_is_checked_before_accepting(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    path = tmp_path / "bound.tar.gz"
    value(export_snapshot(store, snapshot_id, path))
    prior = (store.root / "current-snapshot.json").read_bytes()
    assert isinstance(import_snapshot(store, path, expected_snapshot_id="a" * 64), Err)
    assert isinstance(import_snapshot(store, path, expected_producer_revision="a" * 40), Err)
    assert (store.root / "current-snapshot.json").read_bytes() == prior


def _bind_inventory_recipes(store, snapshot_id, case_recipe, inventory_recipe):
    from bulk_archive import references
    payload = value(store.load("snapshot", snapshot_id))
    payload["producer"]["inventory_recipe_sha256"] = case_recipe
    snapshot_id = value(store.accept("snapshot", payload, list(references(payload))))
    inventory = add_inventory(store)
    inventory["producer"] = {**inventory.get("producer", {}), "recipe_sha256": inventory_recipe}
    value(store.accept("inventory", inventory, list(references(inventory))))
    return snapshot_id


def test_export_refuses_inventory_from_a_recipe_not_retained_by_the_case_producer(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    mixed = _bind_inventory_recipes(store, snapshot_id, "a" * 64, "b" * 64)
    path = tmp_path / "mixed-recipes.tar.gz"
    assert isinstance(export_snapshot(store, mixed, path), Err)
    assert not path.exists()


def test_archive_validation_rejects_a_hash_consistent_mixed_recipe_manifest(tmp_path):
    import hashlib
    from bulk_archive import inspect_archive
    from bulk_common import load_json
    store, snapshot_id, _, _ = fixture(tmp_path)
    bound = _bind_inventory_recipes(store, snapshot_id, "a" * 64, "a" * 64)
    good, bad = tmp_path / "bound-recipes.tar.gz", tmp_path / "mixed-recipes.tar.gz"
    value(export_snapshot(store, bound, good))
    with tarfile.open(good, "r:gz") as source, tarfile.open(bad, "w:gz", format=tarfile.USTAR_FORMAT) as target:
        for entry in source.getmembers():
            raw = source.extractfile(entry).read()
            if entry.name == "archive.json":
                bundle = load_json(raw)
                payload = bundle["inventory"]["payload"]
                payload["producer"]["recipe_sha256"] = "b" * 64
                bundle["inventory"]["id"] = hashlib.sha256(canonical_bytes(payload)).hexdigest()
                raw = canonical_bytes(bundle)
                entry.size = len(raw)
            target.addfile(entry, io.BytesIO(raw))
    pointer = (store.root / "current-snapshot.json").read_bytes()
    assert isinstance(inspect_archive(bad), Err)
    assert isinstance(import_snapshot(store, bad), Err)
    assert (store.root / "current-snapshot.json").read_bytes() == pointer
