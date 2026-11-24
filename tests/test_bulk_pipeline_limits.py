"""Whole inventory acceptance must preserve its replay and producer contracts."""
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from bulk_common import Err, Ok
from bulk_http import HttpArtifact
from bulk_pipeline import capture_native_inventory, replay_inventory
from bulk_store import Store


def _page(index=0, *, oversized=False):
    count = 1000 if oversized else 0
    entries = []
    for number in range(count):
        key = f"bulk-data/randoms/{index * 1000 + number:05d}-" + "x" * 430 + ".csv"
        entries.append(f'<Contents><Key>{key}</Key><Size>100</Size><ETag>"' + "a" * 32 + '"</ETag><LastModified>2026-09-30T09:00:24.000Z</LastModified></Contents>')
    truncated = oversized and index < 4
    tokens = f"<ContinuationToken>{index}</ContinuationToken>" if index else ""
    if truncated:
        tokens += f"<NextContinuationToken>{index + 1}</NextContinuationToken>"
    raw = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>com-courtlistener-storage</Name><Prefix>bulk-data/</Prefix>'
           f'<KeyCount>{count}</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>{str(truncated).lower()}</IsTruncated>' + tokens + "".join(entries) + "</ListBucketResult>").encode()
    assert len(raw) < 1024**2
    return raw


def _fetcher(*, oversized=False):
    def fetch(url, maximum, **_options):
        assert maximum == 1024**2
        token = parse_qs(urlsplit(url).query).get("continuation-token", [None])[0]
        raw = _page(0 if token is None else int(token), oversized=oversized)
        return Ok(HttpArtifact(url, 200, {"content-type": "application/xml"}, raw))
    return fetch


def _baseline(tmp_path: Path):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    first = capture_native_inventory(store, fetcher=_fetcher())
    assert isinstance(first, Ok), first
    payload = store.load("inventory")
    assert isinstance(payload, Ok), payload
    assert isinstance(replay_inventory(store, payload.value), Ok)
    return store, payload.value


def test_oversized_derived_inventory_cannot_replace_a_replayable_pointer(tmp_path: Path):
    store, original = _baseline(tmp_path)
    pointer = (tmp_path / "current-inventory.json").read_bytes()
    result = capture_native_inventory(store, fetcher=_fetcher(oversized=True))
    assert isinstance(result, Err), "Five valid bounded pages must not accept metadata larger than the replay reader's 4 MiB bound"
    assert "metadata" in result.error.lower() and ("limit" in result.error.lower() or "bound" in result.error.lower())
    assert (tmp_path / "current-inventory.json").read_bytes() == pointer
    assert store.load("inventory").value == original
    assert isinstance(replay_inventory(store, original), Ok)


def test_inventory_replay_binds_and_requires_the_recorded_parser_recipe(tmp_path: Path):
    store, payload = _baseline(tmp_path)
    altered = {**payload, "producer": {**payload.get("producer", {}), "recipe_sha256": "0" * 64}}
    assert isinstance(replay_inventory(store, altered), Err), "A different recorded native parser recipe must not replay as the current one"
    missing = {key: value for key, value in payload.items() if key != "producer"}
    assert isinstance(replay_inventory(store, missing), Err)
    assert re.fullmatch(r"[a-f0-9]{64}", payload["producer"]["recipe_sha256"])
