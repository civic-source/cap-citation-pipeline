"""Native source inventories preserve exact pages without claiming body coverage."""
from html import escape
from urllib.parse import parse_qs, urlsplit

import pytest

from bulk_common import Err, Ok
from bulk_http import HttpArtifact
from bulk_inventory import capture_inventory, parse_inventory_pages

OBSERVED = "2026-11-24T17:00:00Z"
CORE = ["courts", "dockets", "opinion-clusters", "opinions", "schema"]


def source(table, date="2026-09-30"):
    suffix = "sql" if table == "schema" else "csv.bz2"
    return {"key": f"bulk-data/{table}-{date}.{suffix}", "size": "100", "etag": '"' + "a" * 32 + '"', "lastModified": "2026-09-30T09:00:24.000Z"}


def page(objects, *, truncated=False, next_token=None, token=None):
    items = "".join(f"<Contents><Key>{escape(row['key'])}</Key><Size>{row['size']}</Size><ETag>{escape(row['etag'])}</ETag><LastModified>{row['lastModified']}</LastModified><StorageClass>STANDARD</StorageClass></Contents>" for row in objects)
    markers = (f"<NextContinuationToken>{escape(next_token)}</NextContinuationToken>" if next_token is not None else "")
    markers += (f"<ContinuationToken>{escape(token)}</ContinuationToken>" if token is not None else "")
    return (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>com-courtlistener-storage</Name><Prefix>bulk-data/</Prefix><KeyCount>{len(objects)}</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>{str(truncated).lower()}</IsTruncated>{markers}{items}</ListBucketResult>').encode()


def test_replays_complete_metadata_and_selects_the_latest_common_generation():
    rows = [source(table) for table in CORE] + [source("citations"), source("opinions", "2026-10-31")]
    rows.append({**source("courts"), "key": "bulk-data/eyecite/tests/one-percent.csv.bz2"})
    result = parse_inventory_pages([page(rows)], OBSERVED)
    assert isinstance(result, Ok)
    value = result.value
    assert value["latestCommonDate"] == "2026-09-30"
    assert value["selectedTables"]["opinions"] == "bulk-data/opinions-2026-09-30.csv.bz2"
    assert value["missingOpinionContents"] is True
    assert value["sourceCoverage"] == {"kind": "inventory-only", "objectBodiesCollected": False}
    assert len(value["objects"]) == 8
    assert value["objects"] == sorted(value["objects"], key=lambda row: row["key"])


def test_capture_encodes_continuation_values_and_retains_exact_pages():
    token = "opaque+/=&?https://example.invalid/"
    pages = [page([source("courts")], truncated=True, next_token=token), page([source(table) for table in CORE[1:]], token=token)]
    requested = []

    def fetcher(url, cap, **_options):
        requested.append((url, cap))
        return Ok(HttpArtifact(url, 200, {"content-type": "application/xml"}, pages[len(requested) - 1]))

    result = capture_inventory(fetcher=fetcher, observed_at=OBSERVED)
    assert isinstance(result, Ok)
    assert result.value.raw_pages == tuple(pages)
    assert result.value.manifest == parse_inventory_pages(pages, OBSERVED).value
    assert all(urlsplit(url).hostname == "com-courtlistener-storage.s3.us-west-2.amazonaws.com" for url, _ in requested)
    assert parse_qs(urlsplit(requested[1][0]).query)["continuation-token"] == [token]
    assert all(cap == 1024**2 for _, cap in requested)


@pytest.mark.parametrize("change", [
    {"key": "bulk-data/../escape.csv.bz2"},
    {"key": "bulk-data/opinions-2026-02-30.csv.bz2"},
    {"key": "bulk-data/evil%2fkey.csv.bz2"},
    {"size": "1.5"}, {"size": "-1"}, {"size": str(2**53)},
    {"etag": "not-an-etag"}, {"lastModified": "2026-02-30T09:00:00Z"},
])
def test_rejects_untrusted_object_identities(change):
    assert isinstance(parse_inventory_pages([page([{**source("opinions"), **change}])], OBSERVED), Err)


def test_rejects_duplicate_keys_and_broken_pagination_chains():
    row = source("courts")
    assert isinstance(parse_inventory_pages([page([row, row])], OBSERVED), Err)
    assert isinstance(parse_inventory_pages([page([row], truncated=True, next_token="next")], OBSERVED), Err)
    assert isinstance(parse_inventory_pages([page([row]), page([])], OBSERVED), Err)
    assert isinstance(parse_inventory_pages([page([row], truncated=True, next_token="next"), page([], token="wrong")], OBSERVED), Err)


@pytest.mark.parametrize("payload", [
    b'<!DOCTYPE ListBucketResult [<!ENTITY x "unsafe">]><ListBucketResult>&x;</ListBucketResult>',
    page([])[:-3], page([]).replace(b"com-courtlistener-storage", b"other-bucket"),
    page([]).replace(b"bulk-data/", b"other-prefix/"),
    page([]).replace(b"2006-03-01", b"unknown"),
    b"x" * (1024**2 + 1),
], ids=["dtd", "truncated-xml", "wrong-bucket", "wrong-prefix", "wrong-namespace", "oversized-page"])
def test_rejects_unsafe_or_malformed_metadata(payload):
    assert isinstance(parse_inventory_pages([payload], OBSERVED), Err)


def test_does_not_accept_partial_or_not_modified_inventory_as_a_fresh_capture():
    def fetcher(url, _cap, **_options):
        return Ok(HttpArtifact(url, 304, {}, b""))
    assert isinstance(capture_inventory(fetcher=fetcher, observed_at=OBSERVED), Err)
    assert isinstance(parse_inventory_pages([page([])], "2026-02-30T00:00:00Z"), Err)


def test_capture_limits_page_count_without_following_untrusted_urls():
    requested = []

    def fetcher(url, _cap, **_options):
        requested.append(url)
        token = parse_qs(urlsplit(url).query).get("continuation-token", [None])[0]
        number = len(requested)
        raw = page([source(f"table{number}")], truncated=True, next_token=f"next{number}", token=token)
        return Ok(HttpArtifact(url, 200, {"content-type": "application/xml"}, raw))

    assert isinstance(capture_inventory(fetcher=fetcher, observed_at=OBSERVED), Err)
    assert len(requested) == 10


def test_duplicate_identity_fields_and_page_count_mismatches_fail_closed():
    raw = page([source("courts")])
    duplicate = raw.replace(b"<Size>100</Size>", b"<Size>100</Size><Size>100</Size>")
    assert isinstance(parse_inventory_pages([duplicate], OBSERVED), Err)
    assert isinstance(parse_inventory_pages([raw.replace(b"<KeyCount>1", b"<KeyCount>2")], OBSERVED), Err)
