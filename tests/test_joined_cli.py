"""Separate operator commands preserve the pilot namespace and explicit failures."""
import json

from bulk_common import Err, Ok
import joined


def test_joined_plan_names_exact_source_and_resource_limits(tmp_path, capsys):
    assert joined.main(["--action", "plan", "--cache", str(tmp_path / "cache")]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["source"]["expected_length"] == 959035282
    assert value["limits"]["preflight_bytes"] == 10 * 1024**3
    assert value["limits"]["worker_address_bytes"] == 4 * 1024**3
    assert not (tmp_path / "cache" / "current-snapshot.json").exists()


def test_joined_capture_has_explicit_cohort_and_failure_exit_without_network(tmp_path, capsys, monkeypatch):
    calls = []

    def capture(store, config):
        calls.append((store, config))
        return Err("fixture refused")

    monkeypatch.setattr(joined, "capture_joined", capture)
    assert joined.main(["--action", "capture", "--cache", str(tmp_path / "cache"), "--sections", "111,841"]) == 1
    assert calls[0][1]["sections"] == ["111", "841"]
    assert calls[0][0]._reserve == 4 * 1024**3
    assert "fixture refused" in capsys.readouterr().err


def test_rebuild_uses_joined_snapshot_and_never_needs_cohort(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(joined, "rebuild_joined", lambda store, identifier: Ok({"snapshot_id": "a" * 64}))
    assert joined.main(["--action", "rebuild", "--cache", str(tmp_path / "cache")]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == "a" * 64


def test_capture_reuses_verified_stored_joined_cohort_with_explicit_since(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(joined.Store, "load", lambda self, kind, identifier=None: Ok({"configuration": {"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": None}}))
    monkeypatch.setattr(joined, "capture_joined", lambda store, config: calls.append(config) or Ok({"snapshot_id": "b" * 64}))
    assert joined.main(["--action", "capture", "--cache", str(tmp_path / "cache"), "--since", "2020-01-01"]) == 0
    assert calls == [{"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": "2020-01-01"}]


def test_plan_reports_preflight_not_ready_without_claiming_capacity_is_sufficient(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(joined.Store, "capacity", lambda self: Ok({"cacheBytes": 0, "freeBytes": 1, "maxCacheBytes": 16 * 1024**3, "minFreeBytes": 4 * 1024**3}))
    monkeypatch.setattr(joined.Store, "ensure_capacity", lambda self, additional: Err("Insufficient filesystem space"))
    assert joined.main(["--action", "plan", "--cache", str(tmp_path / "cache")]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["ready"] is False
    assert "Insufficient" in value["not_ready_reason"]
