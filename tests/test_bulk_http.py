"""Bounded HTTP artifacts are exercised against a real local server."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from bulk_common import Err, Ok
from bulk_http import HttpPolicy, fetch_bytes


@contextmanager
def server(status=200, body=b"source", headers=None):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/source", paths
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def local(url, cap, **kwargs):
    return fetch_bytes(url, cap, policy=HttpPolicy(frozenset({"127.0.0.1"}), True), **kwargs)


def test_retains_exact_bytes_and_lowercase_headers():
    with server(headers={"Content-Length": "6", "ETag": '"identity"'}) as (url, paths):
        result = local(url, 6)
    assert isinstance(result, Ok)
    assert result.value.body == b"source"
    assert result.value.headers["etag"] == '"identity"'
    assert result.value.url == url
    assert paths == ["/source"]


@pytest.mark.parametrize("headers,body", [({}, b"1234567"), ({"Content-Length": "7"}, b"1234567"), ({"Content-Length": "10"}, b"short")])
def test_rejects_oversized_or_truncated_bodies(headers, body):
    with server(body=body, headers=headers) as (url, _):
        assert isinstance(local(url, 6), Err)


def test_declines_redirects_without_requesting_the_new_location():
    with server(status=302, headers={"Location": "/redirect-target"}) as (url, paths):
        assert isinstance(local(url, 100), Err)
    assert paths == ["/source"]


def test_untrusted_urls_are_rejected_before_a_request():
    with server() as (url, paths):
        assert isinstance(fetch_bytes(url, 100), Err)
        assert isinstance(local(url.replace("http://", "http://user:secret@"), 100), Err)
    assert paths == []


def test_conditional_not_modified_retains_no_body_even_with_representation_length():
    with server(status=304, headers={"Content-Length": "12345"}) as (url, _):
        result = local(url, 1, headers={"If-None-Match": '"identity"'})
    assert isinstance(result, Ok)
    assert result.value.status == 304
    assert result.value.body == b""


def test_unsolicited_not_modified_and_encoded_payloads_fail_closed():
    with server(status=304) as (url, _):
        assert isinstance(local(url, 10), Err)
    with server(headers={"Content-Encoding": "gzip"}) as (url, _):
        assert isinstance(local(url, 10), Err)


def test_validates_requested_range_before_accepting_a_partial_artifact():
    with server(status=206, body=b"abc", headers={"Content-Range": "bytes 0-2/10", "Content-Length": "3"}) as (url, _):
        assert isinstance(local(url, 3, headers={"Range": "bytes=0-2"}), Ok)
        assert isinstance(local(url, 3), Err)
    with server(status=200, body=b"abc") as (url, _):
        assert isinstance(local(url, 3, headers={"Range": "bytes=0-2"}), Err)


@pytest.mark.parametrize("limit", [0, -1, True, 32 * 1024**2 + 1])
def test_enforces_maximum_fetch_budget_before_network(limit):
    with server() as (url, paths):
        assert isinstance(local(url, limit), Err)
    assert paths == []


def test_short_body_fails_even_when_advertised_size_is_within_budget():
    with server(body=b"short", headers={"Content-Length": "6"}) as (url, _):
        assert isinstance(local(url, 6), Err)


def test_malformed_request_options_return_errors_before_network():
    with server() as (url, paths):
        assert isinstance(local(url, 10, headers=["not-a-header-map"]), Err)
        assert isinstance(local(url, 10, headers={"X-Test": "injected\r\nheader"}), Err)
    assert paths == []
