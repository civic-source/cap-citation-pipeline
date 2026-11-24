"""Real Parquet capture, multipart reconstruction and independent worker replay."""

from pathlib import Path

from bulk_common import Blob, Err, Ok
from bulk_store import Store
from joined_pipeline import capture_joined, rebuild_joined
from multipart_archive import export_joined, import_joined
from test_joined_pipeline import fixture_source
from test_joined_projection import CONFIG


def value(result):
    assert isinstance(result, Ok), result
    return result.value


def test_actual_reader_rebuilds_multipart_source_and_keeps_pilot_namespace(tmp_path):
    spec, fetcher, calls = fixture_source(tmp_path)
    original = Store(tmp_path / "original", min_free_bytes=0, free_provider=lambda: 100 * 1024**3)
    captured = value(capture_joined(original, CONFIG, source=spec, fetcher=fetcher))
    exported = value(export_joined(original, captured["snapshot_id"], tmp_path / "archive", part_size=1024))
    assert len(exported["parts"]) > 1
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    value(restored.init())
    value(restored.accept("snapshot", {"unrelated": True}, []))
    prior = (restored.root / "current-snapshot.json").read_bytes()
    selected = value(import_joined(restored, Path(exported["index_path"])))
    assert selected == captured["snapshot_id"]
    rebuilt = value(rebuild_joined(restored))
    assert rebuilt["snapshot_id"] == selected
    assert rebuilt["coverage"]["records_with_matches"] == 1
    assert (restored.root / "current-snapshot.json").read_bytes() == prior
    assert len(calls) == 1


def test_hash_valid_but_semantically_wrong_product_cannot_select_snapshot(tmp_path):
    spec, fetcher, _ = fixture_source(tmp_path)
    original = Store(tmp_path / "original", min_free_bytes=0, free_provider=lambda: 100 * 1024**3)
    captured = value(capture_joined(original, CONFIG, source=spec, fetcher=fetcher))
    payload = value(original.load("joined-snapshot", captured["snapshot_id"]))
    payload["product"]["coverage"]["records_scanned"] += 1
    references = [Blob(**payload["source"]["blob"]),
                  *[Blob(**part["blob"]) for part in payload["product"]["partitions"]]]
    wrong = value(original.accept("joined-snapshot", payload, references))
    exported = value(export_joined(original, wrong, tmp_path / "archive", part_size=1024))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    value(restored.init())
    result = import_joined(restored, Path(exported["index_path"]))
    assert isinstance(result, Err) and "projection differs" in result.error
    assert not (restored.root / "current-joined-snapshot.json").exists()


def test_restored_source_can_refilter_without_another_acquisition(tmp_path):
    spec, fetcher, _ = fixture_source(tmp_path)
    original = Store(tmp_path / "original", min_free_bytes=0, free_provider=lambda: 100 * 1024**3)
    captured = value(capture_joined(original, CONFIG, source=spec, fetcher=fetcher))
    exported = value(export_joined(original, captured["snapshot_id"], tmp_path / "archive", part_size=1024))
    restored = Store(tmp_path / "restored", min_free_bytes=0, free_provider=lambda: 100 * 1024**3)
    value(restored.init())
    value(import_joined(restored, Path(exported["index_path"])))
    network = []
    def blocked_fetch(*_):
        network.append(True)
        return Err("network disabled")
    result = capture_joined(restored, {**CONFIG, "since": "2000-01-01"}, source=spec, fetcher=blocked_fetch)
    assert isinstance(result, Ok), result
    assert not network
    assert value(restored.load("joined-source"))["source"]["blob"] == captured["source"]
