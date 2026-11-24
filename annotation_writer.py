"""Bounded, atomic annotation output that preserves prior editorial decisions."""
import fcntl
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

MAX_SIDECAR_BYTES = 4 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class AnnotationWriteError(ValueError):
    """An annotation cannot be safely validated or replaced."""


class _PriorLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise AnnotationWriteError("Duplicate key in prior annotation YAML")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_PriorLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _validate_case(case) -> None:
    for field, limit in (("caseName", 500), ("citation", 200), ("holdingSummary", 500)):
        if not isinstance(case.get(field), str) or len(case[field]) > limit:
            raise AnnotationWriteError(f"Prior annotation has an invalid {field}")
    if not isinstance(case.get("date"), str) or not isinstance(case.get("sourceUrl"), str):
        raise AnnotationWriteError("Prior annotation has invalid date or source URL fields")
    if case["sourceUrl"] and not re.match(r"^https?://", case["sourceUrl"], re.IGNORECASE):
        raise AnnotationWriteError("Prior annotation source URL must use HTTP or HTTPS")
    if case.get("court") not in {"SCOTUS", "Appellate", "District"}:
        raise AnnotationWriteError("Prior annotation has an invalid court")
    if case.get("impact") not in {"unverified_mention", "interpretation", "unconstitutional", "narrowed", "historical"}:
        raise AnnotationWriteError("Prior annotation has an invalid impact")


def _validate_prior(value: object, target: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("targetSection") != target:
        raise AnnotationWriteError("Prior annotation has a missing or mismatched targetSection")
    if not _valid_timestamp(value.get("lastSyncedET")):
        raise AnnotationWriteError("Prior annotation has an invalid lastSyncedET")
    cases = value.get("cases")
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise AnnotationWriteError("Prior annotation cases must be a list of mappings")
    count = value.get("totalCases", len(cases))
    if type(count) is not int or count < 0:
        raise AnnotationWriteError("Prior annotation has an invalid case count")
    for case in cases:
        _validate_case(case)
        if case.get("extractionState") not in (None, "candidate", "verified", "rejected"):
            raise AnnotationWriteError("Prior annotation has an invalid extraction state")
        if case.get("editorialReview") not in (None, "unreviewed", "accepted", "disputed", "superseded"):
            raise AnnotationWriteError("Prior annotation has an invalid editorial review state")
    return value


def _read_prior(directory: int, filename: str, target: str):
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None, None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AnnotationWriteError("Prior annotation must be a regular file with one link")
        if info.st_size > MAX_SIDECAR_BYTES:
            raise AnnotationWriteError("Prior annotation exceeds the byte limit")
        raw = source.read(MAX_SIDECAR_BYTES + 1)
        if len(raw) > MAX_SIDECAR_BYTES:
            raise AnnotationWriteError("Prior annotation exceeds the byte limit")
    try:
        parsed = yaml.load(raw.decode("utf-8"), Loader=_PriorLoader)
        return _validate_prior(parsed, target), info
    except (yaml.YAMLError, UnicodeError, TypeError) as error:
        raise AnnotationWriteError(f"Cannot read prior annotation YAML: {error}") from error


def _reviewed(prior: dict[str, Any]) -> bool:
    return any(case.get("extractionState") == "verified" or
               case.get("editorialReview") not in (None, "unreviewed")
               for case in prior["cases"])


def _refresh_metadata(data, target: str, observed_at: str, last_success: str, retained: bool):
    status = data.get("status", "success-nonempty")
    result = {"source": "courtlistener", "scope": target, "status": status,
              "observedAt": observed_at, "lastSuccessAt": last_success,
              "isStale": retained or status not in {"success-nonempty", "success-empty"},
              "retainedPriorSnapshot": retained,
              "completedPages": data.get("completedPages", 0 if retained else 1),
              "expectedPages": data.get("expectedPages", 1)}
    for field in ("failureKind", "errorMessage"):
        if data.get(field):
            result[field] = data[field]
    return result


def _replacement(data, prior, target: str, now: str, max_cases: int):
    status = data.get("status", "success-nonempty")
    observed_at = data.get("observedAt") or now
    cases = data.get("cases", [])
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise AnnotationWriteError("Incoming annotation cases must be a list of mappings")
    if status == "failed" or (status == "partial" and not cases):
        if prior is None:
            return None
        prior["refreshStatus"] = _refresh_metadata(data, target, observed_at, prior["lastSyncedET"], True)
        return prior
    stale = status not in {"success-nonempty", "success-empty"}
    last_success = prior["lastSyncedET"] if stale and prior else observed_at
    return {"targetSection": target, "lastSyncedET": observed_at,
            "totalCases": data.get("totalCases", len(cases)),
            "refreshStatus": _refresh_metadata(data, target, observed_at, last_success, False),
            "cases": cases[:max_cases]}


def _check_unchanged(directory: int, filename: str, prior_info) -> None:
    current = os.stat(filename, dir_fd=directory, follow_symlinks=False)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
    if not stat.S_ISREG(current.st_mode) or any(getattr(current, field) != getattr(prior_info, field) for field in fields):
        raise AnnotationWriteError("Prior annotation changed during validation")


def _publish(directory: int, filename: str, annotation, prior_info) -> None:
    content = yaml.safe_dump(annotation, default_flow_style=False, allow_unicode=True, sort_keys=False).encode("utf-8")
    if len(content) > MAX_SIDECAR_BYTES:
        raise AnnotationWriteError("Annotation output exceeds the byte limit")
    temporary = f".{filename}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if prior_info is None:
            os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
        else:
            _check_unchanged(directory, filename, prior_info)
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _write_one(root: int, key: str, data, now: str, max_cases: int) -> int:
    matched = re.fullmatch(r"title-([1-9][0-9]*)/section-([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)", key)
    if not matched or not isinstance(data, dict):
        raise AnnotationWriteError("Invalid annotation scope or acquisition outcome")
    title, section = matched.groups()
    target, directory_name = f"{title} U.S.C. § {section}", f"title-{title}"
    try:
        os.mkdir(directory_name, dir_fd=root)
    except FileExistsError:
        pass
    directory = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=root)
    try:
        filename = f"section-{section}.yaml"
        prior, info = _read_prior(directory, filename, target)
        if prior is not None and _reviewed(prior):
            print(f"Refusing to overwrite reviewed annotation: {key}", file=sys.stderr)
            return 0
        if data.get("status") == "success-empty":
            return 0
        replacement = _replacement(data, prior, target, now, max_cases)
        if replacement is None:
            return 0
        _publish(directory, filename, replacement, info)
        return 1
    finally:
        os.close(directory)


def write_annotations(results, output_dir: Path, now: str, max_cases: int = 20) -> int:
    """Write automatic candidates while preserving reviewed or unreadable prior data."""
    output_dir.mkdir(parents=True, exist_ok=True)
    root = os.open(output_dir, _DIRECTORY_FLAGS)
    try:
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return sum(_write_one(root, key, data, now, max_cases) for key, data in results.items())
    finally:
        os.close(root)
