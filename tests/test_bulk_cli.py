"""Public operator commands preserve explicit snapshot and resource choices."""

import hashlib
import json
from pathlib import Path

import bulk
from bulk_common import Err, Ok, canonical_bytes
from bulk_http import HttpArtifact
from bulk_pipeline import SourceSpec, capture_sample
from bulk_store import Store


def test_status_reports_the_requested_historical_snapshot(tmp_path: Path, capsys):
    row = {
        "id": 1,
        "case_name": "Fixture",
        "date_filed": "2020-01-01",
        "court_type": "F",
        "court_jurisdiction": "USA, Federal",
        "court_full_name": "Court of Appeals for the Ninth Circuit",
        "court_short_name": "Ninth Circuit",
        "citations": ["100 F.4th 1"],
        "opinions": [
            {"opinion_id": 2, "opinion_text": "18 U.S.C. § 111.", "type": "020lead", "ocr": False}
        ],
    }
    raw = canonical_bytes(row)
    source = SourceSpec(
        "0" * 40,
        "2023-10-19",
        len(raw),
        hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
    )
    store = Store(tmp_path, min_free_bytes=0)
    store.init()
    config = {"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": None}
    first = capture_sample(
        store, config, source=source, fetcher=lambda u, *a, **kw: Ok(HttpArtifact(u, 200, {}, raw))
    )
    assert isinstance(first, Ok)
    second = capture_sample(
        store,
        {**config, "since": "2019-01-01"},
        source=source,
        fetcher=lambda *a, **kw: Err("No network"),
    )
    assert isinstance(second, Ok) and first.value["snapshot_id"] != second.value["snapshot_id"]
    assert (
        bulk.main(
            [
                "--action",
                "status",
                "--cache",
                str(tmp_path),
                "--snapshot",
                first.value["snapshot_id"],
                "--min-free-gib",
                "0",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == first.value["snapshot_id"]


def test_plan_refuses_a_small_quota_without_fetching_or_creating_objects(tmp_path: Path, capsys):
    assert (
        bulk.main(
            [
                "--action",
                "plan",
                "--cache",
                str(tmp_path),
                "--max-cache-gib",
                "0",
                "--min-free-gib",
                "0",
            ]
        )
        == 1
    )
    assert "quota" in capsys.readouterr().err
    assert not list((tmp_path / "objects").iterdir())
