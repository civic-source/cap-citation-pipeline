"""Bounded deterministic partitions and transient disk-backed source identities."""
import hashlib
import os
from pathlib import Path
import sqlite3

from bulk_common import Blob, canonical_bytes


class Output:
    def __init__(self, directory, limits):
        self.directory, self.limits = Path(directory), limits
        self.partitions, self.current = [], None
        self.total, self.sequence, self.count, self.size = 0, 0, 0, 0
        self.group, self.first, self.digest = None, 0, hashlib.sha256()
        self.db = sqlite3.connect(self.directory / "identities.sqlite")
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA page_size=4096")
        self.db.execute(f"PRAGMA max_page_count={limits['index_bytes'] // 4096}")
        self.db.execute("CREATE TABLE identities (kind TEXT, id INTEGER, digest TEXT, PRIMARY KEY(kind,id)) WITHOUT ROWID")

    def unique(self, kind, identifier, value):
        self.capacity(65536)
        digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
        found = self.db.execute("SELECT digest FROM identities WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        if found is not None:
            if found[0] != digest:
                raise ValueError(f"Contradictory {kind} identity")
            return False
        self.db.execute("INSERT INTO identities VALUES (?,?,?)", (kind, identifier, digest))
        self.check_index()
        return True

    def capacity(self, additional):
        filesystem = os.statvfs(self.directory)
        if filesystem.f_bavail * filesystem.f_frsize - self.limits["min_free_bytes"] < additional:
            raise ValueError("Insufficient worker filesystem reserve")

    def check_index(self):
        total = sum(path.stat().st_size for path in self.directory.glob("identities.sqlite*"))
        if total > self.limits["index_bytes"]:
            raise ValueError("Identity index exceeds byte budget")

    def emit(self, group, record):
        raw = canonical_bytes(record)
        self.capacity(len(raw))
        if len(raw) > self.limits["partition_bytes"] or self.total + len(raw) > self.limits["output_bytes"]:
            raise ValueError("Joined evidence exceeds output byte budget")
        if self.current is not None and (self.group != group or self.size + len(raw) > self.limits["partition_bytes"]):
            self.flush()
        if self.current is None:
            self.current = open(self.directory / "partition.tmp", "xb")
            self.group, self.first = group, self.sequence
        self.current.write(raw)
        self.digest.update(raw)
        self.total, self.size = self.total + len(raw), self.size + len(raw)
        self.sequence, self.count = self.sequence + 1, self.count + 1

    def flush(self):
        if self.current is None:
            return
        self.current.flush()
        os.fsync(self.current.fileno())
        self.current.close()
        blob = Blob(self.digest.hexdigest(), self.size)
        os.rename(self.directory / "partition.tmp", self.directory / blob.sha256)
        self.partitions.append({"row_group": self.group, "first_sequence": self.first, "record_count": self.count, "blob": blob.to_dict()})
        if len(self.partitions) > 4000:
            raise ValueError("Too many joined partitions")
        self.current, self.count, self.size, self.digest = None, 0, 0, hashlib.sha256()

    def close(self):
        self.db.close()
        if self.current is not None:
            self.current.close()
        for path in self.directory.glob("identities.sqlite*"):
            path.unlink()
