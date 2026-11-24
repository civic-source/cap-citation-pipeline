"""Joined checkpoints, offline replay and isolated acceptance use actual files."""
from datetime import datetime, timezone

import pytest

from bulk_common import Blob, Err, Ok
from bulk_store import Store
from joined_pipeline import capture_joined, rebuild_joined
from joined_source import SourceSpec
from test_joined_projection import CONFIG, make_parquet, row


def fixture_source(tmp_path, rows=None, raw=None):
    path = tmp_path / "input.parquet"
    if raw is None:
        make_parquet(path, rows or [row()])
        raw = path.read_bytes()
    blob = Blob.of(raw)
    spec = SourceSpec("a" * 40, "part-00000-00000000-0000-0000-0000-000000000000-c000.gz.parquet", len(raw), blob.sha256)
    calls = []

    def fetcher(store, source):
        calls.append(source)
        stored = store.put(raw)
        assert isinstance(stored, Ok), stored
        return Ok({**source.descriptor(), "blob": stored.value.to_dict(),
                   "acquired_at": datetime.now(timezone.utc).isoformat(), "headers": {}})
    return spec, fetcher, calls


def cache(tmp_path):
    return Store(tmp_path / "cache", min_free_bytes=0, free_provider=lambda: 100 * 1024**3)


def test_complete_capture_reuses_checkpoint_and_replays_offline_without_pilot_pointer(tmp_path):
    source, fetcher, calls = fixture_source(tmp_path)
    store = cache(tmp_path)
    first = capture_joined(store, CONFIG, source=source, fetcher=fetcher)
    assert isinstance(first, Ok), first
    second = capture_joined(store, CONFIG, source=source, fetcher=lambda *a, **k: Err("network blocked"))
    assert isinstance(second, Ok), second
    assert second.value["snapshot_id"] == first.value["snapshot_id"]
    assert len(calls) == 1
    assert not (store.root / "current-snapshot.json").exists()
    assert isinstance(rebuild_joined(store), Ok)
    snapshot = store.load("joined-snapshot").value
    assert snapshot["product"]["coverage"]["records_with_matches"] == 1
    assert snapshot["producer"]["pyarrow_version"] == "23.0.1"


def test_hash_valid_malformed_source_is_cached_but_no_snapshot_selected(tmp_path):
    source, fetcher, calls = fixture_source(tmp_path, raw=b"not parquet")
    store = cache(tmp_path)
    assert isinstance(capture_joined(store, CONFIG, source=source, fetcher=fetcher), Err)
    assert isinstance(capture_joined(store, CONFIG, source=source, fetcher=fetcher), Err)
    assert len(calls) == 1
    assert isinstance(store.load("joined-source"), Ok)
    assert not (store.root / "current-joined-snapshot.json").exists()


def test_tampered_product_replay_and_budget_refusal_preserve_prior_selected_bytes(tmp_path):
    source, fetcher, calls = fixture_source(tmp_path)
    store = cache(tmp_path)
    assert isinstance(capture_joined(store, CONFIG, source=source, fetcher=fetcher), Ok)
    prior = (store.root / "current-joined-snapshot.json").read_bytes()
    snapshot = store.load("joined-snapshot").value
    reference = Blob(**snapshot["product"]["partitions"][0]["blob"])
    store.path(reference).write_bytes(b"corrupt")
    assert isinstance(rebuild_joined(store), Err)
    assert (store.root / "current-joined-snapshot.json").read_bytes() == prior
    refused = Store(tmp_path / "small", min_free_bytes=0, max_cache_bytes=1024)
    assert isinstance(capture_joined(refused, CONFIG, source=source, fetcher=fetcher), Err)
    assert len(calls) == 1


def test_invalid_configuration_refuses_before_any_source_fetch(tmp_path):
    source, fetcher, calls = fixture_source(tmp_path)
    assert isinstance(capture_joined(cache(tmp_path), {**CONFIG, "sections": []}, source=source, fetcher=fetcher), Err)
    assert not calls


def test_worker_failure_after_source_checkpoint_preserves_prior_pointer(tmp_path, monkeypatch):
    import joined_pipeline
    source, fetcher, _ = fixture_source(tmp_path)
    store = cache(tmp_path)
    assert isinstance(capture_joined(store, CONFIG, source=source, fetcher=fetcher), Ok)
    prior = (store.root / "current-joined-snapshot.json").read_bytes()
    monkeypatch.setattr(joined_pipeline, "project_file", lambda *a, **kw: Err("worker exhausted"))
    failed = capture_joined(store, {**CONFIG, "since": "2021-01-01"}, source=source, fetcher=fetcher)
    assert isinstance(failed, Err)
    assert (store.root / "current-joined-snapshot.json").read_bytes() == prior
    assert isinstance(store.load("joined-source"), Ok)


@pytest.mark.parametrize("observation", [b" " * 8193, b'{"peak_rss_bytes":1,"elapsed_seconds":1e999}'], ids=["oversized", "nonfinite"])
def test_oversized_worker_observation_is_bounded_before_parse_on_every_capture_path(tmp_path, monkeypatch, observation):
    import joined_pipeline
    source, fetcher, _ = fixture_source(tmp_path)
    store = cache(tmp_path)
    assert isinstance(capture_joined(store, CONFIG, source=source, fetcher=fetcher), Ok)
    prior = (store.root / "current-joined-snapshot.json").read_bytes()
    original = joined_pipeline._project
    unbounded = []
    original_read = type(store.root).read_text

    def read_text(path, *args, **kwargs):
        if path.name == "resources.json":
            unbounded.append(path)
        return original_read(path, *args, **kwargs)

    def oversize(*args):
        product = original(*args)
        (args[-1] / "resources.json").write_bytes(observation)
        return product

    monkeypatch.setattr(type(store.root), "read_text", read_text)
    monkeypatch.setattr(joined_pipeline, "_project", oversize)
    for config in [CONFIG, {**CONFIG, "since": "2021-01-01"}]:
        failed = capture_joined(store, config, source=source, fetcher=fetcher)
        assert isinstance(failed, Err)
        assert any(word in failed.error.lower() for word in ("bound", "limit", "observation"))
        assert (store.root / "current-joined-snapshot.json").read_bytes() == prior
    assert not unbounded
