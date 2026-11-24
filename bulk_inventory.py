"""Retain and validate native CourtListener S3 inventory metadata only."""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import re
from urllib.parse import urlencode
from xml.parsers import expat

from bulk_common import Err, Ok, Result
from bulk_http import fetch_bytes

BASE = "https://com-courtlistener-storage.s3.us-west-2.amazonaws.com"
NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/}"
PAGE_LIMIT = 1024**2
PAGE_COUNT_LIMIT = 10
CORE_TABLES = ("courts", "dockets", "opinion-clusters", "opinions", "schema")


@dataclass(frozen=True)
class NativeInventory:
    manifest: dict[str, object]
    raw_pages: tuple[bytes, ...]


@dataclass
class _Node:
    tag: str
    children: list["_Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    text_size: int = 0


class _SecureXml:
    def __init__(self):
        self.stack = []
        self.root = None
        self.nodes = 0
        self.parser = expat.ParserCreate(namespace_separator="}")
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.characters
        self.parser.StartDoctypeDeclHandler = self.reject
        self.parser.EntityDeclHandler = self.reject
        self.parser.ExternalEntityRefHandler = self.reject
        self.parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    @staticmethod
    def reject(*_args):
        raise ValueError("DTD and entity declarations are prohibited in inventory metadata")

    def start(self, tag, attributes):
        self.nodes += 1
        if not tag.startswith(NAMESPACE) or attributes or len(self.stack) >= 8 or self.nodes > 16000:
            raise ValueError("Unexpected inventory XML namespace, attributes or structure bounds")
        node = _Node(tag.removeprefix(NAMESPACE))
        if self.stack:
            self.stack[-1].children.append(node)
        elif self.root is not None:
            raise ValueError("Multiple inventory roots")
        else:
            self.root = node
        self.stack.append(node)

    def end(self, _tag):
        self.stack.pop()

    def characters(self, value):
        if self.stack:
            node = self.stack[-1]
            node.text_size += len(value.encode("utf-8"))
            if node.text_size > 8192:
                raise ValueError("Inventory text field exceeds its byte limit")
            node.text.append(value)


def _field(node, name, required=True):
    matches = [child for child in node.children if child.tag == name]
    if len(matches) > 1 or (required and not matches):
        raise ValueError(f"Missing or duplicate inventory field {name}")
    if not matches:
        return None
    if matches[0].children:
        raise ValueError("Inventory scalar field contains nested elements")
    return "".join(matches[0].text)


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
        raise ValueError("Inventory timestamps must be explicit UTC values")
    datetime.fromisoformat(value)
    return value


def _object(node):
    key, length = _field(node, "Key"), _field(node, "Size")
    etag, modified = _field(node, "ETag"), _timestamp(_field(node, "LastModified"))
    if not re.fullmatch(r"bulk-data/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", key) or len(key) > 512:
        raise ValueError("Invalid native source object key")
    if any(part in {".", ".."} for part in key.split("/")) or len(key.split("/")) > 5:
        raise ValueError("Unsafe native source object key")
    dated = re.search(r"-(\d{4}-\d{2}-\d{2})(?=\.)", key)
    if dated:
        date.fromisoformat(dated[1])
    if not re.fullmatch(r"0|[1-9][0-9]*", length) or int(length) > 2**53 - 1:
        raise ValueError("Native object length is not a safe nonnegative integer")
    if not re.fullmatch(r'"[a-fA-F0-9]{32}(?:-[1-9][0-9]{0,5})?"', etag):
        raise ValueError("Invalid native object ETag")
    return {"key": key, "url": BASE + "/" + key, "size": int(length), "etag": etag, "lastModified": modified}


def _parse_page(raw):
    if not isinstance(raw, bytes) or len(raw) > PAGE_LIMIT:
        raise ValueError("Inventory page exceeds its byte limit")
    raw.decode("utf-8", errors="strict")
    xml = _SecureXml()
    xml.parser.Parse(raw, True)
    root = xml.root
    if root is None or root.tag != "ListBucketResult" or _field(root, "Name") != "com-courtlistener-storage" or _field(root, "Prefix") != "bulk-data/":
        raise ValueError("Inventory bucket or prefix identity mismatch")
    allowed = {"Name", "Prefix", "KeyCount", "MaxKeys", "IsTruncated", "Contents", "ContinuationToken", "NextContinuationToken"}
    if any(child.tag not in allowed for child in root.children):
        raise ValueError("Unexpected native inventory page field")
    contents = [child for child in root.children if child.tag == "Contents"]
    count, maximum = _field(root, "KeyCount"), _field(root, "MaxKeys")
    if not re.fullmatch(r"0|[1-9][0-9]*", count) or int(count) != len(contents) or maximum != "1000" or len(contents) > 1000:
        raise ValueError("Inventory page count is inconsistent")
    truncated = _field(root, "IsTruncated")
    token, following = _field(root, "ContinuationToken", False), _field(root, "NextContinuationToken", False)
    if truncated not in {"true", "false"} or (truncated == "true") != bool(following):
        raise ValueError("Inventory pagination marker is inconsistent")
    if any(value is not None and (not value or any(ord(char) < 32 for char in value)) for value in [token, following]):
        raise ValueError("Invalid inventory continuation token")
    if truncated == "true" and not contents:
        raise ValueError("Truncated inventory page contains no objects")
    return {"objects": [_object(node) for node in contents], "token": token, "following": following, "truncated": truncated == "true"}


def _selection(objects):
    by_table = {table: {} for table in (*CORE_TABLES, "citations", "opinion-contents")}
    for item in objects:
        match = re.fullmatch(r"bulk-data/([a-z-]+)-(\d{4}-\d{2}-\d{2})\.(csv\.bz2|sql)", item["key"])
        if not match or match[1] not in by_table:
            continue
        table, stamp, extension = match.groups()
        if extension != ("sql" if table == "schema" else "csv.bz2"):
            raise ValueError("Native table filename has an inconsistent format")
        if item["size"] > (0 if table == "schema" else 14):
            by_table[table][stamp] = item["key"]
    dates = set(by_table[CORE_TABLES[0]])
    for table in CORE_TABLES[1:]:
        dates.intersection_update(by_table[table])
    latest = max(dates) if dates else None
    selected = {table: generations[latest] for table, generations in by_table.items() if latest in generations}
    return latest, selected, "opinion-contents" not in selected


def parse_inventory_pages(pages: list[bytes], observed_at: str) -> Result[dict[str, object]]:
    try:
        _timestamp(observed_at)
        if not pages or len(pages) > PAGE_COUNT_LIMIT:
            raise ValueError("Inventory must contain one through ten complete pages")
        objects, keys, tokens = [], set(), set()
        previous = None
        for index, raw in enumerate(pages):
            page = _parse_page(raw)
            if page["token"] != previous or page["truncated"] != (index < len(pages) - 1):
                raise ValueError("Inventory page chain is incomplete or inconsistent")
            for item in page["objects"]:
                if item["key"] in keys:
                    raise ValueError("Duplicate native inventory object key")
                keys.add(item["key"])
                objects.append(item)
            previous = page["following"]
            if previous is not None and previous in tokens:
                raise ValueError("Inventory continuation token repeats")
            tokens.add(previous)
        objects.sort(key=lambda item: item["key"])
        latest, selected, missing = _selection(objects)
        return Ok({"schemaVersion": "cap.courtlistener-inventory/v1", "observedAt": observed_at, "objects": objects,
                   "latestCommonDate": latest, "selectedTables": selected, "missingOpinionContents": missing,
                   "sourceCoverage": {"kind": "inventory-only", "objectBodiesCollected": False}})
    except (ValueError, TypeError, expat.ExpatError) as error:
        return Err(f"Cannot validate native inventory: {error}")


def capture_inventory(*, fetcher=fetch_bytes, observed_at=None) -> Result[NativeInventory]:
    try:
        observed = observed_at if observed_at is not None else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _timestamp(observed)
        pages, tokens, token = [], set(), None
        for _ in range(PAGE_COUNT_LIMIT):
            parameters = {"list-type": "2", "prefix": "bulk-data/", "max-keys": "1000"}
            if token is not None:
                parameters["continuation-token"] = token
            url = BASE + "/?" + urlencode(parameters)
            fetched = fetcher(url, PAGE_LIMIT, headers={"Accept": "application/xml"})
            if isinstance(fetched, Err):
                return fetched
            artifact = fetched.value
            if artifact.url != url or artifact.status != 200 or artifact.headers.get("content-type", "").split(";")[0].strip().lower() not in {"application/xml", "text/xml"}:
                return Err("Native inventory response identity, status or content type is invalid")
            page = _parse_page(artifact.body)
            if page["token"] != token:
                return Err("Native inventory continuation identity mismatch")
            pages.append(artifact.body)
            if not page["truncated"]:
                parsed = parse_inventory_pages(pages, observed)
                return parsed if isinstance(parsed, Err) else Ok(NativeInventory(parsed.value, tuple(pages)))
            token = page["following"]
            if token in tokens:
                return Err("Native inventory continuation token repeats")
            tokens.add(token)
        return Err("Native inventory exceeded its ten-page budget")
    except (ValueError, TypeError, expat.ExpatError) as error:
        return Err(f"Cannot capture native inventory: {error}")
