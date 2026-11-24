#!/usr/bin/env python3
"""Capture and recover the explicitly qualified historical joined COLD shard."""
import argparse
from pathlib import Path
import re
import sys

from bulk_common import Err, Ok, canonical_bytes
from bulk_store import Store
from joined_pipeline import PREFLIGHT_BYTES, capture_joined, rebuild_joined
from joined_source import APPROVED_SOURCE


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", required=True, choices=["plan", "capture", "rebuild", "status", "export", "import", "archive", "restore"])
    parser.add_argument("--cache", type=Path, default=Path(".cache/joined-precedent"))
    parser.add_argument("--usc-title", default="18")
    parser.add_argument("--sections", help="Explicit comma-separated section cohort")
    parser.add_argument("--data-repo", type=Path)
    parser.add_argument("--since", help="Inclusive reported filing-date filter; not an upstream delta")
    parser.add_argument("--snapshot")
    parser.add_argument("--input", type=Path, help="Canonical multipart index path")
    parser.add_argument("--output", type=Path, help="Exclusive multipart output directory")
    parser.add_argument("--repository", default="civic-source/cap-citation-pipeline")
    parser.add_argument("--tag")
    parser.add_argument("--max-cache-gib", type=int, default=16)
    parser.add_argument("--min-free-gib", type=int, default=4)
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
        sections = sorted({path.stem.removeprefix("section-") for path in directory.rglob("section-*.md")})
    else:
        baseline = store.load("joined-snapshot", args.snapshot)
        if isinstance(baseline, Err):
            raise ValueError("Provide --sections or --data-repo for the first joined cohort")
        from joined_rules import configuration
        settings, _ = configuration(baseline.value["configuration"])
        if settings["usc_title"] != args.usc_title:
            raise ValueError("Stored joined cohort belongs to another USC title")
        return {**settings, "since": args.since if args.since is not None else settings["since"]}
    return {"usc_title": args.usc_title, "sections": sections, "court_types": ["F", "FD"], "since": args.since}


def plan(store):
    status = store.capacity()
    if isinstance(status, Err):
        return status
    readiness = store.ensure_capacity(PREFLIGHT_BYTES)
    return Ok({"source": APPROVED_SOURCE.descriptor(), "ready": isinstance(readiness, Ok),
               "not_ready_reason": readiness.error if isinstance(readiness, Err) else None, "coverage": "one historical shard; global corpus incomplete",
               "limits": {"preflight_bytes": PREFLIGHT_BYTES, "worker_address_bytes": 4 * 1024**3,
                          "output_bytes": 1024**3, "partition_bytes": 32 * 1024**2,
                          "index_bytes": 512 * 1024**2, "archive_part_bytes": 128 * 1024**2},
               "capacity": status.value})


def snapshot_id(store, selected):
    if selected is not None:
        return selected
    import hashlib
    loaded = store.load("joined-snapshot")
    if isinstance(loaded, Err):
        raise ValueError(loaded.error)
    return hashlib.sha256(canonical_bytes(loaded.value)).hexdigest()


def transfer(args, store):
    if args.action in ("export", "import"):
        from multipart_archive import export_joined, import_joined
        if args.action == "export":
            if args.output is None:
                raise ValueError("Provide --output directory")
            return export_joined(store, snapshot_id(store, args.snapshot), args.output)
        if args.input is None:
            raise ValueError("Provide --input index")
        return import_joined(store, args.input)
    from multipart_github import archive_joined_to_github, restore_joined_from_github
    if args.action == "archive":
        return archive_joined_to_github(store, snapshot_id(store, args.snapshot), args.repository)
    if args.tag is None:
        raise ValueError("Provide --tag")
    return restore_joined_from_github(store, args.repository, args.tag)


def dispatch(args, store):
    direct = {"plan": lambda: plan(store), "capture": lambda: capture_joined(store, cohort(args, store)),
              "rebuild": lambda: rebuild_joined(store, args.snapshot),
              "status": lambda: store.load("joined-snapshot", args.snapshot)}
    if args.action in direct:
        return direct[args.action]()
    return store.locked(lambda: transfer(args, store))


def main(argv=None):
    try:
        args = arguments(argv)
        if not 1 <= args.max_cache_gib <= 1024 or not 4 <= args.min_free_gib <= 1024:
            raise ValueError("Positive cache quota and at least 4 GiB reserve required")
        store = Store(args.cache, min_free_bytes=args.min_free_gib * 1024**3, max_cache_bytes=args.max_cache_gib * 1024**3)
        initialized = store.init()
        result = initialized if isinstance(initialized, Err) else dispatch(args, store)
        if isinstance(result, Err):
            print(result.error, file=sys.stderr)
            return 1
        sys.stdout.buffer.write(canonical_bytes(result.value))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
