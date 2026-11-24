#!/usr/bin/env python3
"""Retain, inspect and replay explicitly scoped public precedent sources."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from bulk_common import Blob, Err, Ok, canonical_bytes, load_json
from bulk_pipeline import (
    DEFAULT_SOURCE,
    capture_native_inventory,
    capture_sample,
    current_manifest,
    rebuild_snapshot,
    replay_inventory,
)
from bulk_store import Store
from bulk_producer import producer_identity, verify_recorded_producer


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "plan",
            "inventory",
            "capture",
            "rebuild",
            "verify",
            "status",
            "export",
            "import",
            "archive",
            "restore",
        ],
    )
    parser.add_argument("--cache", type=Path, default=Path(".cache/precedent-bulk"))
    parser.add_argument("--usc-title", default="18")
    parser.add_argument("--sections", help="Explicit comma-separated section identifiers")
    parser.add_argument("--data-repo", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--since", help="Inclusive reported filing-date filter YYYY-MM-DD; not a source delta"
    )
    parser.add_argument("--snapshot")
    parser.add_argument(
        "--offline", action="store_true", help="Replay retained inventory without HTTP"
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repository", default="civic-source/cap-citation-pipeline")
    parser.add_argument("--tag")
    parser.add_argument("--max-cache-gib", type=float, default=16)
    parser.add_argument("--min-free-gib", type=float, default=2)
    return parser.parse_args(argv)


def cohort(args, store):
    if args.sections:
        sections = args.sections.split(",")
    elif args.data_repo:
        if not re.fullmatch(r"[1-9][0-9]*[A-Za-z]?", args.usc_title):
            raise ValueError("Invalid USC title")
        directory = args.data_repo / "statutes" / f"title-{args.usc_title}"
        if not directory.is_dir():
            raise ValueError("USC title directory does not exist")
        sections = sorted(
            {p.stem.removeprefix("section-") for p in directory.rglob("section-*.md")},
            key=lambda value: [int(v) if v.isdigit() else v for v in re.split(r"(\d+)", value)],
        )
    else:
        baseline = store.load("snapshot", args.snapshot)
        if isinstance(baseline, Err):
            raise ValueError("Provide --sections or --data-repo for the first cohort")
        configuration = baseline.value["configuration"]
        if configuration["usc_title"] != args.usc_title:
            raise ValueError("Stored cohort belongs to another USC title")
        sections = configuration["sections"]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        sections = sections[: args.limit]
    return {
        "usc_title": args.usc_title,
        "sections": sections,
        "court_types": ["F", "FD"],
        "since": args.since,
    }


def inventory_summary(payload):
    selected = set(payload["selectedTables"].values())
    return {
        "latest_common_date": payload["latestCommonDate"],
        "object_count": len(payload["objects"]),
        "selected_tables": payload["selectedTables"],
        "selected_compressed_bytes": sum(
            value["size"] for value in payload["objects"] if value["key"] in selected
        ),
        "missing_opinion_contents": payload["missingOpinionContents"],
        "source_coverage": payload["sourceCoverage"],
    }


def inventory_action(args, store):
    if not args.offline:
        captured = capture_native_inventory(store)
        return (
            captured
            if isinstance(captured, Err)
            else Ok(
                {
                    "inventory_id": captured.value["inventory_id"],
                    **inventory_summary(captured.value["inventory"]),
                }
            )
        )
    payload = store.load("inventory")
    if isinstance(payload, Err):
        return payload
    replayed = replay_inventory(store, payload.value)
    if isinstance(replayed, Err):
        return replayed
    ref = Blob(**payload.value["metadata"])
    raw = store.read(ref)
    if isinstance(raw, Err):
        return raw
    return Ok(
        {
            "inventory_id": hashlib.sha256(canonical_bytes(payload.value)).hexdigest(),
            "replay_verified": True,
            **inventory_summary(load_json(raw.value)),
        }
    )


def status_action(args, store):
    capacity = store.capacity()
    if isinstance(capacity, Err):
        return capacity
    result = {"cache": str(store.root), "capacity": capacity.value}
    snapshot = None
    for kind in ("source", "inventory", "snapshot"):
        payload = (
            store.load(kind, args.snapshot)
            if kind == "snapshot" and args.snapshot
            else current_manifest(store, kind)
        )
        if isinstance(payload, Err):
            return payload
        result[kind + "_id"] = (
            hashlib.sha256(canonical_bytes(payload.value)).hexdigest() if payload.value else None
        )
        if kind == "snapshot":
            snapshot = payload.value
    if snapshot is not None:
        product = store.read(Blob(**snapshot["product"]))
        if isinstance(product, Err):
            return product
        decoded = load_json(product.value)
        result.update(
            coverage=decoded["coverage"],
            match_count=len(decoded["matches"]),
            matched_sections=len({match["section"] for match in decoded["matches"]}),
        )
    return Ok(result)


def archive_action(args, store):
    from bulk_archive import export_snapshot, import_snapshot
    from bulk_github import archive_to_github, restore_from_github

    if args.action == "import":
        if args.input is None:
            return Err("--input is required")
        return store.locked(lambda: import_snapshot(store, args.input))
    if args.action == "restore":
        if not args.tag:
            return Err("--tag is required")
        return store.locked(lambda: restore_from_github(store, args.repository, args.tag))
    payload = store.load("snapshot", args.snapshot)
    if isinstance(payload, Err):
        return payload
    identifier = hashlib.sha256(canonical_bytes(payload.value)).hexdigest()
    if args.action == "export":
        if args.output is None:
            return Err("--output is required")
        return store.locked(lambda: export_snapshot(store, identifier, args.output))
    if producer_identity()["revision"] is None:
        return Err("Commit the producer/adapter code before GitHub archival")
    verified = verify_recorded_producer(payload.value)
    if isinstance(verified, Err):
        return verified
    return store.locked(lambda: archive_to_github(store, identifier, args.repository))


def plan_action(store):
    required = 4 * (DEFAULT_SOURCE.expected_length + 64 * 1024**2) + 4 * 1024**2
    capacity = store.capacity()
    if isinstance(capacity, Err):
        return capacity
    checked = store.ensure_capacity(required)
    return (
        checked
        if isinstance(checked, Err)
        else Ok(
            {
                "source": DEFAULT_SOURCE.descriptor(),
                "transfer_bytes": DEFAULT_SOURCE.expected_length,
                "required_additional_bytes": required,
                "capacity": capacity.value,
                "coverage": "Historical joined sample only; native current corpus remains separate",
            }
        )
    )


def rebuild_action(args, store):
    replayed = store.locked(lambda: rebuild_snapshot(store, args.snapshot))
    if isinstance(replayed, Err):
        return replayed
    inventory = current_manifest(store, "inventory")
    if isinstance(inventory, Err):
        return inventory
    if inventory.value is not None:
        checked = replay_inventory(store, inventory.value)
        if isinstance(checked, Err):
            return checked
    return replayed


def execute(args, store):
    if args.action == "plan":
        return plan_action(store)
    if args.action == "inventory":
        return inventory_action(args, store)
    if args.action == "capture":
        return capture_sample(store, cohort(args, store))
    if args.action == "status":
        return status_action(args, store)
    if args.action in ("rebuild", "verify"):
        return rebuild_action(args, store)
    return archive_action(args, store)


def main(argv=None):
    try:
        args = arguments(argv)
        for value in (args.max_cache_gib, args.min_free_gib):
            if not 0 <= value <= 1024 * 1024:
                raise ValueError("Disk budgets must be finite nonnegative GiB")
        store = Store(
            args.cache,
            min_free_bytes=int(args.min_free_gib * 1024**3),
            max_cache_bytes=int(args.max_cache_gib * 1024**3),
        )
        initialized = store.init()
        if isinstance(initialized, Err):
            raise ValueError(initialized.error)
        result = execute(args, store)
        if isinstance(result, Err):
            print(result.error, file=sys.stderr)
            return 1
        value = result.value.to_dict() if isinstance(result.value, Blob) else result.value
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
