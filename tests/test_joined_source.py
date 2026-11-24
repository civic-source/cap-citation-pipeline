"""Acquisition must retain verified source independently of transformations."""
from dataclasses import replace
import io
from urllib.error import URLError

import pytest

from bulk_common import Blob, Err, Ok
from bulk_store import Store
from joined_source import APPROVED_SOURCE, acquire_source, download_source


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        from email.message import Message
        self.headers = Message()
        for key, value in (headers or {"Content-Length": str(len(body))}).items():
            self.headers[key] = value
        self.status = status


def fixture(tmp_path, raw=b"PAR1fixturePAR1"):
    store = Store(tmp_path, min_free_bytes=0)
    assert isinstance(store.init(), Ok)
    source = replace(APPROVED_SOURCE, expected_length=len(raw), publisher_sha256=Blob.of(raw).sha256)
    return store, source, raw


def test_stream_adoption_and_checkpoint_are_reusable_without_network(tmp_path):
    store, source, raw = fixture(tmp_path)
    calls = []
    def fetcher(store, source):
        calls.append(source.url)
        return download_source(store, source, opener=lambda req: Response(raw))
    first = acquire_source(store, source=source, fetcher=fetcher)
    assert isinstance(first, Ok), first
    assert first.value["blob"] == Blob.of(raw).to_dict()
    assert acquire_source(store, source=source, fetcher=lambda *a: pytest.fail("network")) == first
    assert len(calls) == 1
    assert not (tmp_path / "current-joined-snapshot.json").exists()
    assert not list(store.staging.iterdir())


@pytest.mark.parametrize("url", ["http://huggingface.co/x", "https://127.0.0.1/x", "https://huggingface.co.evil.test/x", "https://user@huggingface.co/x", "https://huggingface.co:444/x", "https://huggingface.co/x#fragment", "https://huggingface.co/\nx"])
def test_malicious_redirect_is_never_followed(tmp_path, url):
    store, source, raw = fixture(tmp_path)
    calls = []
    def opener(req):
        calls.append(req.full_url)
        return Response(b"", 302, {"Location": url})
    assert isinstance(download_source(store, source, opener=opener), Err)
    assert len(calls) == 1
    assert not list(store.staging.iterdir())


def test_redirect_is_bounded_and_does_not_propagate_credentials(tmp_path):
    store, source, raw = fixture(tmp_path)
    calls = []
    def opener(req):
        calls.append(req)
        if len(calls) == 1:
            return Response(b"", 302, {"Location": "https://cas-bridge.xethub.hf.co/public?signature=private"})
        assert "Authorization" not in req.headers and "Cookie" not in req.headers
        assert req.headers["Accept-encoding"] == "identity"
        return Response(raw, headers={"Content-Length": str(len(raw)), "ETag": '"safe"'})
    value = download_source(store, source, opener=opener)
    assert isinstance(value, Ok), value
    assert "private" not in str(value.value)
    assert value.value["url"] == source.url


@pytest.mark.parametrize("body,headers", [(b"short", {"Content-Length": "15"}), (b"long"*30, {"Content-Length": "15"}), (b"PAR1fixturePAR1", {"Content-Length": "14"}), (b"PAR1fixturePAR1", {"Content-Length": "15", "Content-Encoding": "gzip"}), (b"PAR1fixturePAR1", {"Content-Length": "15", "Content-Range": "bytes 0-13/14"}), (b"wrong contents!", {"Content-Length": "15"})])
def test_short_long_encoded_range_and_hash_failures_never_adopt(tmp_path, body, headers):
    store, source, raw = fixture(tmp_path)
    result = download_source(store, source, opener=lambda req: Response(body, headers=headers))
    assert isinstance(result, Err), result
    assert not list(store.staging.iterdir())
    assert not list((tmp_path / "objects").iterdir())


def test_quota_refusal_occurs_before_network(tmp_path):
    store, source, raw = fixture(tmp_path)
    store._quota = 1
    assert isinstance(download_source(store, source, opener=lambda r: pytest.fail("network")), Err)


def test_symlink_staging_is_rejected_without_writing_target(tmp_path):
    store, source, raw = fixture(tmp_path / "cache")
    target = tmp_path / "outside"
    target.mkdir()
    store.staging.rmdir()
    store.staging.symlink_to(target, target_is_directory=True)
    assert isinstance(download_source(store, source, opener=lambda req: Response(raw)), Err)
    assert not list(target.iterdir())


def test_corrupt_checkpoint_or_blob_is_not_silently_downloaded(tmp_path):
    store, source, raw = fixture(tmp_path)
    first = acquire_source(store, source=source, fetcher=lambda s, spec: download_source(s, spec, opener=lambda req: Response(raw)))
    assert isinstance(first, Ok), first
    path = store.path(Blob.of(raw))
    path.write_bytes(b"x" * len(raw))
    assert isinstance(acquire_source(store, source=source, fetcher=lambda *a: pytest.fail("network")), Err)


def test_existing_invalid_pointer_and_wrong_checkpoint_schema_refuse(tmp_path):
    store, source, raw = fixture(tmp_path)
    (tmp_path / "current-joined-source.json").write_bytes(b"not-json")
    assert isinstance(acquire_source(store, source=source, fetcher=lambda *a: pytest.fail("network")), Err)


def test_production_transport_cannot_change_the_approved_source(tmp_path):
    store, source, raw = fixture(tmp_path)
    assert isinstance(download_source(store, source), Err)


def test_transport_exception_does_not_reveal_signed_url(tmp_path):
    store, source, raw = fixture(tmp_path)
    def fail(req):
        raise URLError("https://cas-bridge.xethub.hf.co/?signature=SECRET")
    result = download_source(store, source, opener=fail)
    assert isinstance(result, Err) and "SECRET" not in result.error


def test_duplicate_headers_and_redirect_loop_are_bounded(tmp_path):
    store, source, raw = fixture(tmp_path)
    response = Response(raw)
    response.headers["Content-Length"] = str(len(raw))
    assert isinstance(download_source(store, source, opener=lambda req: response), Err)
    calls = []
    def redirect(req):
        calls.append(req.full_url)
        return Response(b"", 302, {"Location": source.url})
    assert isinstance(download_source(store, source, opener=redirect), Err)
    assert len(calls) == 6


def test_capacity_loss_midstream_cleans_partial_file(tmp_path):
    store, source, raw = fixture(tmp_path)
    calls = []
    def capacity(additional):
        calls.append(additional)
        return Ok(None) if len(calls) == 1 else Err("Filesystem reserve exhausted")
    store.ensure_capacity = capacity
    result = download_source(store, source, opener=lambda req: Response(raw))
    assert isinstance(result, Err) and "reserve" in result.error
    assert not list(store.staging.iterdir())


def test_unverified_injected_descriptor_cannot_create_checkpoint(tmp_path):
    store, source, raw = fixture(tmp_path)
    descriptor = {**source.descriptor(), "blob": Blob.of(raw).to_dict(),
                  "acquired_at": "2026-11-24T00:00:00+00:00", "headers": {}}
    result = acquire_source(store, source=source, fetcher=lambda *a: Ok(descriptor))
    assert isinstance(result, Err)
    assert not (tmp_path / "current-joined-source.json").exists()


def test_mismatched_descriptor_identity_does_not_create_checkpoint(tmp_path):
    store, source, raw = fixture(tmp_path)
    downloaded = download_source(store, source, opener=lambda req: Response(raw))
    assert isinstance(downloaded, Ok)
    downloaded.value["publisher_sha256"] = "0" * 64
    assert isinstance(acquire_source(store, source=source, fetcher=lambda *a: downloaded), Err)
    assert not (tmp_path / "current-joined-source.json").exists()


def test_exclusive_stage_collision_never_deletes_existing_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import joined_source
    store, source, raw = fixture(tmp_path)
    existing = store.staging / "joined-fixed"
    existing.write_bytes(b"owned by another operation")
    monkeypatch.setattr(joined_source, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    assert isinstance(download_source(store, source, opener=lambda req: Response(raw)), Err)
    assert existing.read_bytes() == b"owned by another operation"


@pytest.mark.parametrize("field,value", [("expected_length", True), ("expected_length", -1), ("expected_length", 1024**3+1), ("revision", "main"), ("path", "../source.parquet"), ("publisher_sha256", "0"*63)])
def test_invalid_publisher_spec_is_refused_before_network(tmp_path, field, value):
    store, source, raw = fixture(tmp_path)
    invalid = replace(source, **{field: value})
    assert isinstance(download_source(store, invalid, opener=lambda req: pytest.fail("network")), Err)


def test_wrong_checkpoint_schema_is_not_replaced(tmp_path):
    store, source, raw = fixture(tmp_path)
    descriptor = download_source(store, source, opener=lambda req: Response(raw)).value
    receipt = {"schema_version": "wrong", "acquired_at": descriptor["acquired_at"], "source": descriptor}
    assert isinstance(store.accept("joined-source", receipt, [Blob.of(raw)]), Ok)
    before = (tmp_path / "current-joined-source.json").read_bytes()
    assert isinstance(acquire_source(store, source=source, fetcher=lambda *a: pytest.fail("network")), Err)
    assert (tmp_path / "current-joined-source.json").read_bytes() == before


def test_symbolic_checkpoint_is_refused_without_network(tmp_path):
    store, source, raw = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b'{"sha256":"' + b'0'*64 + b'"}')
    (tmp_path / "current-joined-source.json").symlink_to(outside)
    assert isinstance(acquire_source(store, source=source, fetcher=lambda *a: pytest.fail("network")), Err)
    assert outside.exists()


def test_exact_official_aws_cdn_redirect_is_accepted_without_broadening_hosts(tmp_path):
    store, source, raw = fixture(tmp_path)
    calls = []
    def opener(request):
        calls.append(request.full_url)
        if len(calls) == 1:
            return Response(b"", 302, {"Location": "https://us.aws.cdn.hf.co/public?signature=private"})
        assert request.full_url.startswith("https://us.aws.cdn.hf.co/")
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return Response(raw)
    result = download_source(store, source, opener=opener)
    assert isinstance(result, Ok), result
    assert result.value["url"] == source.url
    assert "private" not in str(result.value)
