"""Explicit acquisition coverage for a declared literal-search cohort."""
import json
import os
from pathlib import Path
import re
import tempfile

_STATUSES = {"success-empty", "success-nonempty", "partial", "failed"}


def declared_cohort(sections):
    declared = {}
    for title, section in sections:
        if type(title) is not int or title < 1 or not isinstance(section, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", section):
            raise ValueError("Invalid declared search section")
        key = f"title-{title}/section-{section}"
        if key in declared:
            raise ValueError("Duplicate declared search section")
        declared[key] = {"key": key, "scope": f"{title} U.S.C. § {section}",
                         "query": f'"{title} U.S.C. § {section}"'}
    return declared


def _observed_outcome(scope, data):
    if not isinstance(data, dict) or data.get("status") not in _STATUSES:
        raise ValueError("Invalid search acquisition outcome")
    cases = data.get("cases")
    if not isinstance(cases, list) or not isinstance(data.get("observedAt"), str) or not data["observedAt"]:
        raise ValueError("Search outcomes require cases and an observation timestamp")
    count = data.get("totalCases", len(cases))
    if type(count) is not int or count < 0:
        raise ValueError("Invalid search result count")
    if data["status"] == "success-empty" and (count != 0 or cases):
        raise ValueError("Searched-empty status contradicts returned cases/count")
    if data["status"] == "success-nonempty" and not cases:
        raise ValueError("Nonempty status requires collected cases")
    result = {**scope, "status": data["status"], "observedAt": data["observedAt"],
              "totalCases": count, "collectedCases": len(cases)}
    for field in ("httpStatus", "completedPages", "expectedPages", "failureKind", "errorMessage"):
        if data.get(field) is not None:
            result[field] = data[field]
    return result


def build_search_coverage(sections, results):
    """Describe query observations separately from annotation publication."""
    declared = declared_cohort(sections)
    if not isinstance(results, dict) or set(results) - set(declared):
        raise ValueError("Acquisition results exceed the declared search cohort")
    outcomes = [_observed_outcome(scope, results[key]) if key in results else
                {**scope, "status": "not_collected"} for key, scope in declared.items()]
    statuses = [outcome["status"] for outcome in outcomes]
    completed = statuses.count("success-empty") + statuses.count("success-nonempty")
    attempted = len(results)
    state = "not_collected" if not attempted else "failed" if statuses.count("failed") == len(declared) else "complete" if completed == len(declared) else "partial"
    counts = {"expectedCount": len(declared), "attemptedCount": attempted, "completedCount": completed,
              "emptyCount": statuses.count("success-empty"), "nonemptyCount": statuses.count("success-nonempty"),
              "partialCount": statuses.count("partial"), "failedCount": statuses.count("failed"),
              "notCollectedCount": statuses.count("not_collected")}
    return {"schemaVersion": "cap.courtlistener-search-coverage/v1", "source": "courtlistener",
            "coverageKind": "acquisition-outcomes", "scopeKind": "literal-section-search-cohort",
            "coverageStatus": state, "globalCorpusComplete": False, "counts": counts, "outcomes": outcomes}


def require_new_coverage_path(destination: Path) -> None:
    try:
        destination.lstat()
    except FileNotFoundError:
        return
    raise FileExistsError(f"Coverage receipt already exists: {destination}")


def write_search_coverage(receipt, destination: Path) -> None:
    """Publish a complete receipt exclusively; preserve existing output paths."""
    require_new_coverage_path(destination)
    content = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".search-coverage-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(name, destination)
        os.unlink(name)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
