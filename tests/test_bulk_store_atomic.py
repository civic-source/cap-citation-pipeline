"""Selection durability failures preserve prior state or report unknown outcomes."""
import os
from pathlib import Path

import pytest

from bulk_common import Err, Ok
from bulk_store import Store
from multipart_archive import export_joined, import_joined


def accepted(result):
    assert isinstance(result, Ok), result
    return result.value


def prepared(tmp_path, kind="snapshot", prior=True):
    store = Store(tmp_path / "cache", min_free_bytes=0)
    accepted(store.init())
    if prior:
        accepted(store.accept(kind, {"generation": 1}, []))
    pointer = store.root / f"current-{kind}.json"
    return store, pointer, pointer.read_bytes() if prior else None


def fsync_failure(monkeypatch, store, pointer, before, *, persistent=False, foreign=False):
    original = os.fsync
    identity = store.root.stat()
    triggered = []
    foreign_bytes = []

    def fail(descriptor):
        info = os.fstat(descriptor)
        selected = pointer.read_bytes() if pointer.exists() else None
        root = (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
        if root and ((not triggered and selected != before) or (triggered and persistent)):
            triggered.append(True)
            if foreign and len(triggered) == 1:
                replacement = store.root / "foreign-pointer"
                replacement.write_bytes(selected)
                replacement.replace(pointer)
                foreign_bytes.append(selected)
            raise OSError("injected pointer-directory fsync failure")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fail)
    return triggered, foreign_bytes


@pytest.mark.parametrize("prior", [False, True])
def test_accept_rolls_back_one_shot_fsync_failure_after_pointer_replace(tmp_path, monkeypatch, prior):
    store, pointer, before = prepared(tmp_path, prior=prior)
    triggered, _ = fsync_failure(monkeypatch, store, pointer, before)
    result = store.accept("snapshot", {"generation": 2}, [])
    assert isinstance(result, Err) and "fsync failure" in result.error
    assert "selection outcome unknown" not in result.error
    assert triggered
    assert (pointer.read_bytes() if pointer.exists() else None) == before
    assert list(store.staging.iterdir()) == []


def test_persistent_rollback_durability_fault_reports_unknown_selection(tmp_path, monkeypatch):
    store, pointer, before = prepared(tmp_path)
    triggered, _ = fsync_failure(monkeypatch, store, pointer, before, persistent=True)
    result = store.accept("snapshot", {"generation": 2}, [])
    assert isinstance(result, Err) and "selection outcome unknown" in result.error
    assert len(triggered) >= 2
    assert list(store.staging.iterdir()) == []


def test_foreign_pointer_with_same_bytes_is_never_overwritten_during_rollback(tmp_path, monkeypatch):
    store, pointer, before = prepared(tmp_path)
    _, foreign = fsync_failure(monkeypatch, store, pointer, before, foreign=True)
    result = store.accept("snapshot", {"generation": 2}, [])
    assert isinstance(result, Err) and "selection outcome unknown" in result.error
    assert foreign and pointer.read_bytes() == foreign[0]
    assert pointer.read_bytes() != before
    assert list(store.staging.iterdir()) == []


def test_pointer_transaction_refuses_a_competing_store_writer(tmp_path, monkeypatch):
    store, pointer, before = prepared(tmp_path)
    competitor = Store(store.root, min_free_bytes=0)
    original = os.fsync
    identity = store.root.stat()
    competing = []

    def fsync(descriptor):
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
                and pointer.read_bytes() != before and not competing):
            competing.append(None)
            competing[0] = competitor.accept("snapshot", {"generation": "competing"}, [])
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    accepted(store.accept("snapshot", {"generation": 2}, []))
    assert isinstance(competing[0], Err)
    assert accepted(store.load("snapshot")) == {"generation": 2}
    assert list(store.staging.iterdir()) == []


def test_real_multipart_import_preserves_prior_pointer_after_final_fsync_failure(tmp_path, monkeypatch):
    source = Store(tmp_path / "source", min_free_bytes=0)
    accepted(source.init())
    blob = accepted(source.put(b"raw shard bytes"))
    payload = {"schema_version": "cap.joined-snapshot/v1", "source": {"blob": blob.to_dict()},
               "product": {"partitions": []}, "producer": {"revision": None}}
    identifier = accepted(source.accept("joined-snapshot", payload, [blob]))
    export = accepted(export_joined(source, identifier, tmp_path / "transport", part_size=8))
    store, pointer, before = prepared(tmp_path, kind="joined-snapshot")
    triggered, _ = fsync_failure(monkeypatch, store, pointer, before)
    result = import_joined(store, Path(export["index_path"]), replayer=lambda *_: Ok(None))
    assert isinstance(result, Err) and "fsync failure" in result.error
    assert triggered and pointer.read_bytes() == before
    assert accepted(store.load("joined-snapshot")) == {"generation": 1}
    assert list(store.staging.iterdir()) == []


@pytest.mark.parametrize("prior", [False, True])
def test_replace_error_after_rename_still_recovers_owned_prior_selection(tmp_path, monkeypatch, prior):
    store, pointer, before = prepared(tmp_path, prior=prior)
    original = os.replace
    failed = []

    def rename(*args, **kwargs):
        original(*args, **kwargs)
        if args[1] == pointer.name and not failed:
            failed.append(True)
            raise OSError("replace acknowledgement failed")

    monkeypatch.setattr(os, "replace", rename)
    result = store.accept("snapshot", {"generation": 2}, [])
    assert isinstance(result, Err) and "replace acknowledgement failed" in result.error
    assert "selection outcome unknown" not in result.error
    assert (pointer.read_bytes() if pointer.exists() else None) == before
    assert list(store.staging.iterdir()) == []


def test_foreign_rollback_stage_is_preserved_and_selection_is_unknown(tmp_path, monkeypatch):
    store, pointer, before = prepared(tmp_path)
    original = os.fsync
    root = store.root.stat()
    replacement_paths = []

    def fail(descriptor):
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino) == (root.st_dev, root.st_ino)
                and pointer.read_bytes() != before and not replacement_paths):
            backup, = store.staging.glob(".selection-*")
            assert backup.stat().st_mode & 0o077 == 0
            backup.unlink()
            backup.write_bytes(b"foreign rollback staging")
            replacement_paths.append(backup)
            raise OSError("injected rollback replacement")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fail)
    result = store.accept("snapshot", {"generation": 2}, [])
    assert isinstance(result, Err) and "selection outcome unknown" in result.error
    assert replacement_paths[0].read_bytes() == b"foreign rollback staging"


@pytest.mark.parametrize("closed_kind", ["directory", "pointer"])
def test_post_commit_descriptor_close_fault_reports_unknown_selection(tmp_path, monkeypatch, closed_kind):
    store, pointer, before = prepared(tmp_path)
    original = os.close
    root = store.root.stat()
    triggered = []

    def close(descriptor):
        info = os.fstat(descriptor)
        current = pointer.stat()
        wanted = root if closed_kind == "directory" else current
        matches = (info.st_dev, info.st_ino) == (wanted.st_dev, wanted.st_ino)
        original(descriptor)
        if matches and pointer.read_bytes() != before and not triggered:
            triggered.append(True)
            raise OSError("descriptor close acknowledgement failed")

    monkeypatch.setattr(os, "close", close)
    result = store.accept("snapshot", {"generation": 2}, [])
    assert isinstance(result, Err) and "selection outcome unknown" in result.error
    assert triggered
