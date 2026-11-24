"""Exact transformation identities and retained producer commit checks."""

import hashlib
from pathlib import Path
import platform
import re
import subprocess
from bulk_common import Err, Ok

RECIPE_FILES = ("bulk_common.py", "cold_projection.py", "usc_citations.py")
INVENTORY_RECIPE_FILES = ("bulk_common.py", "bulk_inventory.py")
ROOT = Path(__file__).resolve().parent


def recipe_digest(reader=None, files=RECIPE_FILES):
    read = reader if reader is not None else lambda name: (ROOT / name).read_bytes()
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode() + b"\0" + read(name) + b"\0")
    return digest.hexdigest()


def producer_identity():
    revision = None
    try:
        dirty = subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                "bulk*.py",
                "cold_projection.py",
                "usc_citations.py",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if not dirty.strip():
            revision = subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    return {
        "revision": revision,
        "recipe_sha256": recipe_digest(),
        "inventory_recipe_sha256": recipe_digest(files=INVENTORY_RECIPE_FILES),
        "python_version": platform.python_version(),
    }


def verify_recorded_producer(payload):
    try:
        producer = payload["producer"]
        revision = producer["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            return Err("Commit the exact producer and recapture before GitHub archival")

        def read(name):
            return subprocess.check_output(
                ["git", "-C", str(ROOT), "show", revision + ":" + name],
                stderr=subprocess.PIPE,
                timeout=15,
            )

        if recipe_digest(read) != producer["recipe_sha256"]:
            return Err("Recorded producer commit does not retain the exact extraction recipe")
        if recipe_digest(read, INVENTORY_RECIPE_FILES) != producer.get("inventory_recipe_sha256"):
            return Err("Recorded producer does not retain the inventory recipe")
        return Ok(revision)
    except Exception as error:
        return Err("Cannot verify recorded producer: " + str(error))
