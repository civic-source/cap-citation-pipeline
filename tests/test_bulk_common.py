"""Public contracts for deterministic retained artifacts."""

import hashlib

from bulk_common import Blob, canonical_bytes


def test_canonical_bytes_and_blob_identity_are_stable():
    assert canonical_bytes({"z": 1, "a": ["é", None]}) == b'{"a":["\xc3\xa9",null],"z":1}\n'
    blob = Blob.of(b"source")
    assert blob.sha256 == hashlib.sha256(b"source").hexdigest()
    assert blob.byte_length == 6
