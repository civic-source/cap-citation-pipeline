"""Capture/replay exercises actual files and a public HTTP seam."""

import hashlib
from pathlib import Path

from bulk_common import Err, Ok, canonical_bytes
from bulk_http import HttpArtifact
from bulk_pipeline import SourceSpec, capture_sample, rebuild_snapshot
from bulk_store import Store


def sample():
    row = {
        "id": 1,
        "case_name": "Fixture",
        "date_filed": "2020-01-01",
        "date_filed_is_approximate": False,
        "court_type": "F",
        "court_jurisdiction": "USA, Federal",
        "court_full_name": "Court of Appeals for the Ninth Circuit",
        "court_short_name": "Ninth Circuit",
        "citations": ["100 F.4th 1"],
        "opinions": [
            {
                "opinion_id": 2,
                "opinion_text": "The charge cites 18 U.S.C. § 111(a).",
                "type": "020lead",
                "ocr": False,
            }
        ],
    }
    raw = canonical_bytes(row)
    git = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    source = SourceSpec("0" * 40, "2023-10-19", len(raw), git)
    return raw, source


def test_capture_and_offline_replay_preserve_identity_and_never_fetch_latest(tmp_path: Path):
    raw, source = sample()
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    calls = []

    def fetcher(url, max_bytes, **kwargs):
        calls.append(url)
        return Ok(HttpArtifact(url, 200, {"content-type": "text/plain", "etag": '"fixture"'}, raw))

    config = {"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": None}
    first = capture_sample(store, config, source=source, fetcher=fetcher)
    assert isinstance(first, Ok), first
    second = capture_sample(
        store, config, source=source, fetcher=lambda *a, **kw: Err("Networking blocked")
    )
    assert second == first
    assert len(calls) == 1
    assert isinstance(rebuild_snapshot(store), Ok)
    assert store.load("snapshot").value["product"] == first.value["product"]


def test_wrong_publisher_identity_cannot_replace_an_accepted_snapshot(tmp_path: Path):
    raw, source = sample()
    store = Store(tmp_path, min_free_bytes=0)
    store.init()
    config = {"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": None}
    first = capture_sample(
        store, config, source=source, fetcher=lambda u, *a, **kw: Ok(HttpArtifact(u, 200, {}, raw))
    )
    assert isinstance(first, Ok), first
    before = (tmp_path / "current-snapshot.json").read_bytes()
    other = SourceSpec("1" * 40, "2023-10-19", len(raw), "f" * 40)
    failed = capture_sample(
        store, config, source=other, fetcher=lambda u, *a, **kw: Ok(HttpArtifact(u, 200, {}, raw))
    )
    assert isinstance(failed, Err)
    assert (tmp_path / "current-snapshot.json").read_bytes() == before


def test_verified_input_survives_a_failed_projection_without_redownloading(tmp_path: Path):
    raw = b"not-json\n"
    source = SourceSpec(
        "0" * 40,
        "2023-10-19",
        len(raw),
        hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
    )
    store = Store(tmp_path, min_free_bytes=0)
    store.init()
    calls = []

    def fetcher(url, *args, **kwargs):
        calls.append(url)
        return Ok(HttpArtifact(url, 200, {}, raw))

    config = {"usc_title": "18", "sections": ["111"], "court_types": ["F", "FD"], "since": None}
    assert isinstance(capture_sample(store, config, source=source, fetcher=fetcher), Err)
    assert isinstance(capture_sample(store, config, source=source, fetcher=fetcher), Err)
    assert len(calls) == 1
    assert not (tmp_path / "current-snapshot.json").exists()
