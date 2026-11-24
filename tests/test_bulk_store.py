"""Real-filesystem acceptance and storage resource contracts."""
from pathlib import Path

from bulk_common import Blob, Err, Ok
from bulk_store import Store


def test_verified_objects_are_immutable_and_capacity_refusal_keeps_current(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0, max_cache_bytes=1024*1024)
    assert isinstance(store.init(), Ok)
    first = store.put(b"source")
    assert isinstance(first, Ok)
    assert store.read(first.value) == Ok(b"source")
    assert isinstance(store.accept("snapshot", {"schemaVersion":"fixture/v1","input":first.value.to_dict()}, [first.value]), Ok)
    before=(tmp_path/'current-snapshot.json').read_bytes()
    assert isinstance(store.ensure_capacity(2*1024*1024), Err)
    assert (tmp_path/'current-snapshot.json').read_bytes() == before
    store.path(first.value).write_bytes(b"tamper")
    assert isinstance(store.put(b"source"), Err)
    assert isinstance(store.accept("snapshot", {"schemaVersion":"fixture/v1"}, [first.value]), Err)
    assert (tmp_path/'current-snapshot.json').read_bytes() == before


def test_low_space_and_concurrent_writer_refuse_before_writing(tmp_path: Path):
    store = Store(tmp_path,min_free_bytes=100,free_provider=lambda:105)
    assert isinstance(store.init(),Ok)
    assert isinstance(store.put(b"123456"),Err)
    assert not list((tmp_path/'objects').iterdir())
    def nested():
        assert isinstance(store.locked(lambda:Ok(None)),Err)
        return Ok(None)
    assert isinstance(store.locked(nested),Ok)


def test_manifest_replay_verifies_nested_references_and_rejects_missing_closure(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    blob = store.put(b"source")
    assert isinstance(blob, Ok)
    payload = {"schemaVersion": "fixture/v1", "parts": [{"raw": blob.value.to_dict()}]}
    accepted = store.accept("snapshot", payload, [blob.value])
    assert isinstance(accepted, Ok)
    assert store.load("snapshot") == Ok(payload)
    assert store.load("snapshot", accepted.value) == Ok(payload)
    before = (tmp_path / "current-snapshot.json").read_bytes()
    assert isinstance(store.accept("snapshot", payload, []), Err)
    assert isinstance(store.accept("snapshot", {"schemaVersion": "other/v1"}, [blob.value]), Err)
    assert (tmp_path / "current-snapshot.json").read_bytes() == before
    store.path(blob.value).unlink()
    assert isinstance(store.load("snapshot"), Err)


def test_adopts_only_verified_files_inside_unlinked_staging_directories(tmp_path: Path):
    import os
    store = Store(tmp_path / "cache", min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    value = b"adopted"
    ref = Blob.of(value)
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "source"
    source.write_bytes(value)
    assert isinstance(store.adopt(source, ref), Err)
    (store.staging / "nested").symlink_to(outside, target_is_directory=True)
    assert isinstance(store.adopt(store.staging / "nested/source", ref), Err)
    assert source.read_bytes() == value
    (store.staging / "nested").unlink()
    staged = store.staging / "source"
    os.link(source, staged)
    assert isinstance(store.adopt(staged, ref), Err)
    staged.unlink()
    staged.write_bytes(value)
    assert store.adopt(staged, ref) == Ok(ref)
    assert store.verify(ref) == Ok(None)
    assert not staged.exists()


def test_existing_corrupt_objects_are_never_overwritten_by_adoption(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    ref = Blob.of(b"original")
    store.path(ref).write_bytes(b"corrupt")
    staged = store.staging / "new"
    staged.write_bytes(b"original")
    assert isinstance(store.adopt(staged, ref), Err)
    assert store.path(ref).read_bytes() == b"corrupt"


def test_read_limits_and_linked_objects_fail_closed(tmp_path: Path):
    import os
    store = Store(tmp_path / "cache", min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    ref = store.put(b"original")
    assert isinstance(ref, Ok)
    assert isinstance(store.read(ref.value, max_bytes=3), Err)
    outside = tmp_path / "outside"
    os.link(store.path(ref.value), outside)
    assert isinstance(store.verify(ref.value), Err)
    assert isinstance(store.capacity(), Err)
    outside.unlink()
    store.path(ref.value).unlink()
    store.path(ref.value).symlink_to(outside)
    assert isinstance(store.read(ref.value), Err)


def test_capacity_accounts_for_retained_and_staged_bytes(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=10, max_cache_bytes=15, free_provider=lambda: 100)
    assert isinstance(store.init(), Ok)
    assert store.put(b"123456") == Ok(Blob.of(b"123456"))
    (store.staging / "interrupted").write_bytes(b"1234")
    capacity = store.capacity()
    assert isinstance(capacity, Ok)
    assert capacity.value == {"cacheBytes": 10, "freeBytes": 100, "minFreeBytes": 10, "maxCacheBytes": 15}
    assert isinstance(store.ensure_capacity(6), Err)


def test_invalid_measurements_and_limits_are_not_accepted(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0, free_provider=lambda: float("nan"))
    assert isinstance(store.init(), Ok)
    assert isinstance(store.capacity(), Err)
    assert isinstance(store.ensure_capacity(-1), Err)
    assert isinstance(store.ensure_capacity(True), Err)


def test_tampered_manifest_or_pointer_cannot_select_unverified_data(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    accepted = store.accept("inventory", {"schemaVersion": "fixture/v1"}, [])
    assert isinstance(accepted, Ok)
    (tmp_path / "manifests" / f"{accepted.value}.json").write_text('{"tamper":true}\n')
    assert isinstance(store.load("inventory"), Err)
    (tmp_path / "current-inventory.json").write_text('{"sha256":"../escape"}\n')
    assert isinstance(store.load("inventory"), Err)


def test_lock_cleanup_survives_work_failure_and_never_releases_other_writer(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    assert store.locked(lambda: Err("expected failure")) == Err("expected failure")
    assert store.locked(lambda: Ok("retry")) == Ok("retry")


def test_failed_new_source_acceptance_preserves_the_prior_complete_selection(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    prior = store.put(b"prior")
    assert isinstance(prior, Ok)
    first_payload = {"input": prior.value.to_dict()}
    assert isinstance(store.accept("snapshot", first_payload, [prior.value]), Ok)
    before = (tmp_path / "current-snapshot.json").read_bytes()
    missing = Blob.of(b"missing")
    assert isinstance(store.accept("snapshot", {"input": missing.to_dict()}, [missing]), Err)
    assert (tmp_path / "current-snapshot.json").read_bytes() == before
    assert store.load("snapshot") == Ok(first_payload)


def test_nested_staging_adoption_and_existing_target_symlink_refusal(tmp_path: Path):
    store = Store(tmp_path / "cache", min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    nested = store.staging / "import"
    nested.mkdir()
    staged = nested / "raw"
    staged.write_bytes(b"nested")
    reference = Blob.of(b"nested")
    assert store.adopt(staged, reference) == Ok(reference)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "raw"
    victim.write_bytes(b"nested")
    nested.rmdir()
    nested.symlink_to(outside, target_is_directory=True)
    assert isinstance(store.adopt(nested / "raw", reference), Err)
    assert victim.read_bytes() == b"nested"


def test_lock_identity_change_never_discards_another_writers_lock(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    def replace_lock():
        path = tmp_path / "writer.lock"
        path.unlink()
        path.write_bytes(b"other writer")
        return Ok(None)
    assert isinstance(store.locked(replace_lock), Err)
    assert (tmp_path / "writer.lock").read_bytes() == b"other writer"


def test_acceptance_canonicalizes_nested_tuples_before_reference_validation(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    missing = Blob.of(b"missing")
    assert isinstance(store.accept("snapshot", {"nested": (missing.to_dict(),)}, []), Err)
    assert not (tmp_path / "current-snapshot.json").exists()


def test_pointer_publication_failure_preserves_prior_selection_and_removes_staging(tmp_path: Path, monkeypatch):
    import os
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    assert isinstance(store.accept("inventory", {"generation": 1}, []), Ok)
    before = (tmp_path / "current-inventory.json").read_bytes()
    def failed_replace(*args, **kwargs):
        raise OSError("Simulated pointer publication failure")
    monkeypatch.setattr(os, "replace", failed_replace)
    assert isinstance(store.accept("inventory", {"generation": 2}, []), Err)
    assert (tmp_path / "current-inventory.json").read_bytes() == before
    assert list(store.staging.iterdir()) == []


def test_initialization_rejects_linked_cache_directories(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "objects").symlink_to(outside, target_is_directory=True)
    assert isinstance(Store(cache).init(), Err)
    assert list(outside.iterdir()) == []


def test_retain_verifies_immutable_manifest_without_selecting_any_pointer(tmp_path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    blob = store.put(b"context").value
    payload = {"context": blob.to_dict()}
    kept = store.retain(payload, [blob])
    assert isinstance(kept, Ok), kept
    assert not list(tmp_path.glob("current-*.json"))
    assert store.load("inventory", kept.value).value == payload
    store.path(blob).write_bytes(b"corrupt")
    assert isinstance(store.retain(payload, [blob]), Err)
    assert not list(tmp_path.glob("current-*.json"))
