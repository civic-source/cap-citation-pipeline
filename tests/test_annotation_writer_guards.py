"""Public filesystem regressions for annotation provenance and protected prior data."""
import os
from pathlib import Path

import pytest
import yaml

import extract

KEY = "title-18/section-111"
TARGET = "18 U.S.C. § 111"
OBSERVED = "2026-11-24T12:00:00Z"
PRIOR = "2026-11-22T12:00:00Z"


def candidate():
    return extract.build_case_annotation(
        {"caseName": "Fresh case", "court_id": "ca1", "snippet": "18 U.S.C. § 111", "absolute_url": "/opinion/1/fresh/"},
        '"18 U.S.C. § 111"', OBSERVED,
    )


def prior_file(root: Path, **markers):
    path = root / "title-18" / "section-111.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    case = {**candidate(), "caseName": "Prior case", **markers}
    data = {"targetSection": TARGET, "lastSyncedET": PRIOR, "totalCases": 2, "cases": [case]}
    path.write_text("# Retain editorial formatting.\n" + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def outcome(status, cases=None):
    return {KEY: {"status": status, "observedAt": OBSERVED, "totalCases": 1,
                  "completedPages": 0 if status == "failed" else 1, "expectedPages": 1,
                  "cases": [candidate()] if cases is None else cases}}


@pytest.mark.parametrize("status,existing", [("success-nonempty", False), ("partial", False), ("failed", True), ("partial", True)])
def test_emits_explicit_refresh_source_and_matching_scope(tmp_path, status, existing):
    if existing:
        prior_file(tmp_path)
    assert extract.write_annotations(outcome(status, [] if existing else None), tmp_path) == 1
    data = yaml.safe_load((tmp_path / "title-18/section-111.yaml").read_text())
    assert data["refreshStatus"]["source"] == "courtlistener"
    assert data["refreshStatus"]["scope"] == data["targetSection"] == TARGET
    assert data["refreshStatus"]["retainedPriorSnapshot"] is existing


@pytest.mark.parametrize("status", ["success-nonempty", "success-empty", "partial", "failed"])
@pytest.mark.parametrize("markers", [{"extractionState": "verified", "editorialReview": "unreviewed"},
                                    {"editorialReview": "accepted"}, {"editorialReview": "disputed"},
                                    {"editorialReview": "superseded"}])
def test_every_refresh_preserves_reviewed_prior_bytes(tmp_path, capsys, status, markers):
    path = prior_file(tmp_path, **markers)
    before = path.read_bytes()
    assert extract.write_annotations(outcome(status, [] if status in {"failed", "success-empty"} else None), tmp_path) == 0
    assert path.read_bytes() == before
    assert "reviewed" in capsys.readouterr().err.lower()


def test_unreviewed_candidate_can_be_replaced(tmp_path):
    path = prior_file(tmp_path)
    assert extract.write_annotations(outcome("success-nonempty"), tmp_path) == 1
    assert yaml.safe_load(path.read_text())["cases"][0]["caseName"] == "Fresh case"


@pytest.mark.parametrize("content", ["cases: [", "not a mapping", "cases: {}", "cases: [42]", "cases: []\ncases: []\n"])
def test_unreadable_prior_fails_closed_without_replacement(tmp_path, content):
    path = tmp_path / "title-18/section-111.yaml"
    path.parent.mkdir()
    path.write_text(content)
    before = path.read_bytes()
    with pytest.raises((ValueError, OSError), match="(?i)annotation|yaml|prior|duplicate"):
        extract.write_annotations(outcome("success-nonempty"), tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_refuses_linked_targets_without_touching_external_bytes(tmp_path, link_kind):
    outside = tmp_path / "outside.yaml"
    outside.write_text("outside must survive")
    root = tmp_path / "output"
    target = root / "title-18/section-111.yaml"
    target.parent.mkdir(parents=True)
    if link_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    with pytest.raises((ValueError, OSError)):
        extract.write_annotations(outcome("success-nonempty"), root)
    assert outside.read_text() == "outside must survive"


def test_refuses_linked_title_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "output"
    root.mkdir()
    (root / "title-18").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        extract.write_annotations(outcome("success-nonempty"), root)
    assert list(outside.iterdir()) == []


def test_failed_fresh_acquisition_creates_no_sidecar(tmp_path):
    assert extract.write_annotations(outcome("failed", []), tmp_path) == 0
    assert not (tmp_path / "title-18/section-111.yaml").exists()


def test_any_reviewed_entry_protects_the_entire_prior_sidecar(tmp_path, capsys):
    path = prior_file(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["cases"].append({**candidate(), "editorialReview": "accepted"})
    path.write_text(yaml.safe_dump(data))
    before = path.read_bytes()
    assert extract.write_annotations(outcome("success-nonempty"), tmp_path) == 0
    assert path.read_bytes() == before
    assert "reviewed" in capsys.readouterr().err.lower()


def test_rejects_mismatched_prior_section_without_replacement(tmp_path):
    path = prior_file(tmp_path)
    path.write_text(path.read_text().replace("18 U.S.C.", "19 U.S.C."))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="targetSection"):
        extract.write_annotations(outcome("success-nonempty"), tmp_path)
    assert path.read_bytes() == before


def test_rejects_oversized_prior_without_reading_or_replacing_it(tmp_path):
    import annotation_writer
    path = prior_file(tmp_path)
    with path.open("ab") as stream:
        stream.truncate(annotation_writer.MAX_SIDECAR_BYTES + 1)
    with pytest.raises(ValueError, match="byte limit"):
        extract.write_annotations(outcome("success-nonempty"), tmp_path)
    assert path.stat().st_size == annotation_writer.MAX_SIDECAR_BYTES + 1


def test_rejects_nonregular_prior_without_blocking_on_a_fifo(tmp_path):
    target = tmp_path / "title-18/section-111.yaml"
    target.parent.mkdir()
    os.mkfifo(target)
    with pytest.raises(ValueError, match="regular file"):
        extract.write_annotations(outcome("success-nonempty"), tmp_path)
    assert target.is_fifo()


def test_atomic_failure_preserves_prior_bytes_and_removes_staging_files(tmp_path, monkeypatch):
    path = prior_file(tmp_path)
    before = path.read_bytes()

    def failed_replace(*args, **kwargs):
        raise OSError("Simulated publication failure")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="publication failure"):
        extract.write_annotations(outcome("success-nonempty"), tmp_path)
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_rejects_linked_output_root_without_touching_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "output"
    output.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        extract.write_annotations(outcome("success-nonempty"), output)
    assert list(outside.iterdir()) == []


def test_refuses_a_concurrent_writer_without_changing_prior_data(tmp_path):
    import fcntl
    path = prior_file(tmp_path)
    before = path.read_bytes()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            extract.write_annotations(outcome("success-nonempty"), tmp_path)
        assert path.read_bytes() == before
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("corruption", ["case-fields", "timestamp", "case-count"])
def test_semantically_unreadable_prior_is_not_replaced(tmp_path, corruption):
    path = prior_file(tmp_path)
    data = yaml.safe_load(path.read_text())
    if corruption == "case-fields":
        data["cases"] = [{}]
    elif corruption == "timestamp":
        data["lastSyncedET"] = "not-a-date"
    else:
        data["totalCases"] = "unknown"
    path.write_text(yaml.safe_dump(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="(?i)prior|annotation"):
        extract.write_annotations(outcome("success-nonempty"), tmp_path)
    assert path.read_bytes() == before
