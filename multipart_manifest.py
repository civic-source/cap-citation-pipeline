"""Closed, deterministic manifests for bounded raw archive parts."""

import hashlib
import re

from bulk_archive import references
from bulk_common import Blob, canonical_bytes

PART_BYTES = 128 * 1024**2
INDEX_BYTES = 4 * 1024**2
TOTAL_BYTES = 4 * 1024**3
MAX_PARTS = 64
MAX_OBJECTS = 4096


def entry(kind, payload):
    return {"kind": kind, "id": hashlib.sha256(canonical_bytes(payload)).hexdigest(), "payload": payload}


def _entry(value, kind, schema):
    if not isinstance(value, dict) or set(value) != {"kind", "id", "payload"}:
        raise ValueError("Invalid multipart manifest entry")
    if value["kind"] != kind or not isinstance(value["payload"], dict):
        raise ValueError("Multipart manifest kind mismatch")
    if value["payload"].get("schema_version") != schema:
        raise ValueError("Multipart payload schema mismatch")
    if value["id"] != hashlib.sha256(canonical_bytes(value["payload"])).hexdigest():
        raise ValueError("Multipart payload identity mismatch")


def _inventory(index):
    if index["inventory"] is None:
        return
    _entry(index["inventory"], "inventory", "cap.native-inventory-snapshot/v1")
    expected = index["snapshot"]["payload"].get("producer", {}).get("inventory_recipe_sha256")
    actual = index["inventory"]["payload"].get("producer", {}).get("recipe_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or expected != actual:
        raise ValueError("Multipart inventory recipe differs from joined producer")


def _object(value, part_size):
    if not isinstance(value, dict) or set(value) != {"sha256", "byte_length", "parts"}:
        raise ValueError("Invalid multipart object")
    reference = Blob(value["sha256"], value["byte_length"])
    if reference.byte_length > TOTAL_BYTES or not isinstance(value["parts"], list):
        raise ValueError("Multipart object exceeds its bound")
    if len(value["parts"]) > MAX_PARTS:
        raise ValueError("Multipart object has too many extents")
    cursor = 0
    for part in value["parts"]:
        if not isinstance(part, dict) or set(part) != {"offset", "blob"}:
            raise ValueError("Invalid multipart extent")
        if type(part["offset"]) is not int or part["offset"] != cursor:
            raise ValueError("Multipart extents must be contiguous and ordered")
        ref = part_blob(part["blob"])
        if ref.byte_length != min(part_size, reference.byte_length - cursor) or ref.byte_length == 0:
            raise ValueError("Multipart extent is not the exact bounded tiling")
        cursor += ref.byte_length
    if cursor != reference.byte_length:
        raise ValueError("Multipart object extent length mismatch")
    if reference.byte_length == 0 and reference != Blob.of(b""):
        raise ValueError("Empty multipart object has an invalid digest")
    return reference


def part_blob(value):
    if not isinstance(value, dict) or set(value) != {"sha256", "byte_length"}:
        raise ValueError("Invalid multipart part reference")
    reference = Blob(**value)
    if not 0 < reference.byte_length <= PART_BYTES:
        raise ValueError("Multipart part exceeds its byte limit")
    return reference


def part_name(reference):
    part_blob(reference.to_dict())
    return f"part-{reference.sha256}.bin"


def part_inventory(index):
    found = {}
    count = 0
    for value in index["objects"]:
        for part in value["parts"]:
            reference = part_blob(part["blob"])
            count += 1
            if count > MAX_PARTS:
                raise ValueError("Multipart archive exceeds its extent budget")
            if reference.sha256 in found and found[reference.sha256] != reference:
                raise ValueError("Conflicting multipart part lengths")
            found[reference.sha256] = reference
    return sorted(found.values(), key=lambda ref: ref.sha256)


def validate_index(index):
    fields = {"schema_version", "part_size", "snapshot", "inventory", "objects"}
    if not isinstance(index, dict) or set(index) != fields or index["schema_version"] != "cap.multipart-archive/v1":
        raise ValueError("Invalid multipart archive index")
    if len(canonical_bytes(index)) > INDEX_BYTES:
        raise ValueError("Multipart index exceeds its byte budget")
    size = index["part_size"]
    if type(size) is not int or not 0 < size <= PART_BYTES:
        raise ValueError("Invalid multipart tiling size")
    _entry(index["snapshot"], "joined-snapshot", "cap.joined-snapshot/v1")
    _inventory(index)
    if not isinstance(index["objects"], list) or len(index["objects"]) > MAX_OBJECTS:
        raise ValueError("Multipart object inventory exceeds its bound")
    actual = [_object(value, size) for value in index["objects"]]
    if actual != sorted(set(actual), key=lambda ref: ref.sha256):
        raise ValueError("Duplicate or unsorted multipart objects")
    expected = references(index["snapshot"]["payload"]) | references(index["inventory"])
    if set(actual) != expected:
        raise ValueError("Multipart payload reference closure mismatch")
    if sum(ref.byte_length for ref in actual) > TOTAL_BYTES:
        raise ValueError("Multipart object closure exceeds its total byte budget")
    part_inventory(index)
    return actual
