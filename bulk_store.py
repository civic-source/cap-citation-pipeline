"""Verified local artifacts and atomic source-collection acceptance."""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Literal
from uuid import uuid4

from bulk_common import Blob, Err, Ok, Result, canonical_bytes, load_json

_MAX_SAFE = (1 << 53) - 1
_MANIFEST_LIMIT = 4 * 1024**2
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _bytes_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_SAFE


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


@contextmanager
def _regular(directory: int, name: str):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Artifact must be a regular file with one link")
        yield source, info
        if _stamp(os.fstat(source.fileno())) != _stamp(info):
            raise ValueError("Artifact changed during verification")


def _read(directory: int, name: str, limit: int) -> bytes:
    with _regular(directory, name) as (source, info):
        if info.st_size > limit:
            raise ValueError("Artifact exceeds byte limit")
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Artifact exceeds byte limit")
        return raw


def _verify(directory: int, name: str, reference: Blob) -> None:
    with _regular(directory, name) as (source, info):
        if info.st_size != reference.byte_length:
            raise ValueError("Artifact byte length mismatch")
        digest, length = hashlib.sha256(), 0
        while chunk := source.read(65536):
            length += len(chunk)
            if length > reference.byte_length:
                raise ValueError("Artifact changed during verification")
            digest.update(chunk)
        if length != reference.byte_length or digest.hexdigest() != reference.sha256:
            raise ValueError("Artifact hash or byte length mismatch")


def _references(value: object) -> set[Blob]:
    found: set[Blob] = set()
    if isinstance(value, dict):
        if "sha256" in value and "byte_length" in value:
            found.add(Blob(value["sha256"], value["byte_length"]))
        for child in value.values():
            found.update(_references(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_references(child))
    lengths: dict[str, int] = {}
    for reference in found:
        if reference.sha256 in lengths and lengths[reference.sha256] != reference.byte_length:
            raise ValueError("Conflicting artifact lengths")
        lengths[reference.sha256] = reference.byte_length
    return found


class Store:
    def __init__(self, root: Path, min_free_bytes: int = 2*1024**3,
                 max_cache_bytes: int = 16*1024**3, free_provider: Callable[[], int] | None = None):
        self.root = Path(os.path.abspath(root))
        self.staging = self.root / "staging"
        self._reserve, self._quota, self._free_provider = min_free_bytes, max_cache_bytes, free_provider

    @contextmanager
    def _parent(self, path: Path) -> Iterator[tuple[int, str]]:
        relative = Path(os.path.abspath(path)).relative_to(self.root)
        if not relative.parts:
            raise ValueError("Artifact path must be inside cache")
        descriptor = os.open(self.root, _DIRECTORY)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, _DIRECTORY, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, relative.parts[-1]
        finally:
            os.close(descriptor)

    def init(self) -> Result[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.root, _DIRECTORY)
            try:
                for name in ("objects", "manifests", "staging"):
                    try:
                        os.mkdir(name, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    os.close(os.open(name, _DIRECTORY, dir_fd=descriptor))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return Ok(None)
        except Exception as error:
            return Err(str(error))

    def path(self, reference: Blob) -> Path:
        Blob(reference.sha256, reference.byte_length)
        if not _bytes_count(reference.byte_length):
            raise ValueError("Invalid artifact byte length")
        return self.root / "objects" / reference.sha256

    def _usage(self, path: Path) -> int:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Cache must contain regular directories")
        total = 0
        with os.scandir(path) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    total += self._usage(Path(entry.path))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                else:
                    raise ValueError("Cache contains a linked or special artifact")
                if not _bytes_count(total):
                    raise ValueError("Cache usage exceeds safe integer bytes")
        return total

    def capacity(self) -> Result[dict]:
        try:
            if not all(_bytes_count(value) for value in (self._reserve, self._quota)):
                raise ValueError("Capacity limits must be nonnegative safe integer bytes")
            used = self._usage(self.root)
            filesystem = os.statvfs(self.root)
            free = self._free_provider() if self._free_provider is not None else filesystem.f_bavail * filesystem.f_frsize
            if not _bytes_count(free):
                raise ValueError("Measured free capacity is invalid")
            return Ok({"cacheBytes": used, "freeBytes": free,
                       "minFreeBytes": self._reserve, "maxCacheBytes": self._quota})
        except Exception as error:
            return Err(str(error))

    def ensure_capacity(self, additional_bytes: int) -> Result[None]:
        if not _bytes_count(additional_bytes):
            return Err("Additional capacity must be nonnegative safe integer bytes")
        status = self.capacity()
        if isinstance(status, Err):
            return status
        value = status.value
        if value["freeBytes"] - self._reserve < additional_bytes:
            return Err("Insufficient filesystem space for artifact and reserve")
        if self._quota - value["cacheBytes"] < additional_bytes:
            return Err("Cache quota would be exceeded")
        return Ok(None)

    def verify(self, reference: Blob) -> Result[None]:
        try:
            with self._parent(self.path(reference)) as (directory, name):
                _verify(directory, name, reference)
            return Ok(None)
        except Exception as error:
            return Err(str(error))

    def read(self, reference: Blob, max_bytes: int = 64*1024**2) -> Result[bytes]:
        try:
            if not _bytes_count(max_bytes) or reference.byte_length > max_bytes:
                raise ValueError("Artifact exceeds read byte limit")
            with self._parent(self.path(reference)) as (directory, name):
                value = _read(directory, name, reference.byte_length)
            if Blob.of(value) != reference:
                raise ValueError("Artifact hash or byte length mismatch")
            return Ok(value)
        except Exception as error:
            return Err(str(error))

    def _discard(self, path: Path) -> None:
        with self._parent(path) as (directory, name):
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _stage(self, content: bytes) -> Path:
        path = self.staging / uuid4().hex
        with self._parent(path) as (directory, name):
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception:
                os.unlink(name, dir_fd=directory)
                raise
        return path

    def put(self, content: bytes) -> Result[Blob]:
        try:
            return self._put(content)
        except Exception as error:
            return Err(str(error))

    def _put(self, content: bytes) -> Result[Blob]:
        staged: Path | None = None
        try:
            if not isinstance(content, bytes):
                raise ValueError("Artifact content must be bytes")
            reference = Blob.of(content)
            with self._parent(self.path(reference)) as (directory, name):
                try:
                    _verify(directory, name, reference)
                    return Ok(reference)
                except FileNotFoundError:
                    pass
            capacity = self.ensure_capacity(len(content))
            if isinstance(capacity, Err):
                return capacity
            staged = self._stage(content)
            return self.adopt(staged, reference)
        except Exception as error:
            return Err(str(error))
        finally:
            if staged is not None:
                self._discard(staged)

    def adopt(self, staged_path: Path, reference: Blob) -> Result[Blob]:
        try:
            staged = Path(os.path.abspath(staged_path))
            if not staged.relative_to(self.staging).parts:
                raise ValueError("Artifact must be inside cache staging")
            with self._parent(staged) as (source, name), self._parent(self.path(reference)) as (target, digest):
                _verify(source, name, reference)
                try:
                    _verify(target, digest, reference)
                except FileNotFoundError:
                    capacity = self.ensure_capacity(0)
                    if isinstance(capacity, Err):
                        return capacity
                    with _regular(source, name) as (staged_file, _):
                        os.fsync(staged_file.fileno())
                    os.link(name, digest, src_dir_fd=source, dst_dir_fd=target, follow_symlinks=False)
                os.unlink(name, dir_fd=source)
                _verify(target, digest, reference)
                os.fsync(source)
                os.fsync(target)
            return Ok(reference)
        except Exception as error:
            return Err(str(error))

    def _atomic(self, path: Path, content: bytes, immutable: bool = False) -> None:
        capacity = self.ensure_capacity(len(content))
        if isinstance(capacity, Err):
            raise ValueError(capacity.error)
        staged = self._stage(content)
        try:
            with self._parent(staged) as (source, name), self._parent(path) as (target, destination):
                try:
                    existing = _read(target, destination, _MANIFEST_LIMIT)
                except FileNotFoundError:
                    existing = None
                if immutable:
                    if existing is not None:
                        if existing != content:
                            raise ValueError("Refusing immutable manifest replacement")
                        return
                    os.link(name, destination, src_dir_fd=source, dst_dir_fd=target, follow_symlinks=False)
                    os.unlink(name, dir_fd=source)
                else:
                    os.replace(name, destination, src_dir_fd=source, dst_dir_fd=target)
                os.fsync(target)
                os.fsync(source)
        finally:
            self._discard(staged)

    def _kind(self, kind: str) -> str:
        if kind not in ("snapshot", "inventory", "source"):
            raise ValueError("Invalid artifact manifest kind")
        return kind

    def accept(self, kind: Literal["snapshot", "inventory", "source"], payload: dict, references: list[Blob]) -> Result[str]:
        try:
            self._kind(kind)
            content = canonical_bytes(payload)
            if len(content) > _MANIFEST_LIMIT or not isinstance(payload, dict):
                raise ValueError("Manifest must be a bounded mapping")
            found = _references(load_json(content))
            if found != set(references):
                raise ValueError("Manifest artifact reference closure mismatch")
            for reference in found:
                verified = self.verify(reference)
                if isinstance(verified, Err):
                    return verified
            digest = hashlib.sha256(content).hexdigest()
            self._atomic(self.root / "manifests" / f"{digest}.json", content, True)
            self._atomic(self.root / f"current-{kind}.json", canonical_bytes({"sha256": digest}))
            return Ok(digest)
        except Exception as error:
            return Err(str(error))

    def load(self, kind: Literal["snapshot", "inventory", "source"], id: str | None = None) -> Result[dict]:
        try:
            self._kind(kind)
            if id is None:
                with self._parent(self.root / f"current-{kind}.json") as (directory, name):
                    pointer = load_json(_read(directory, name, 1024))
                if not isinstance(pointer, dict) or set(pointer) != {"sha256"}:
                    raise ValueError("Invalid current manifest pointer")
                id = pointer["sha256"]
            if not isinstance(id, str) or not re.fullmatch(r"[a-f0-9]{64}", id):
                raise ValueError("Invalid manifest identity")
            with self._parent(self.root / "manifests" / f"{id}.json") as (directory, name):
                raw = _read(directory, name, _MANIFEST_LIMIT)
            payload = load_json(raw)
            if not isinstance(payload, dict) or canonical_bytes(payload) != raw or hashlib.sha256(raw).hexdigest() != id:
                raise ValueError("Manifest digest or canonical representation mismatch")
            for reference in _references(payload):
                verified = self.verify(reference)
                if isinstance(verified, Err):
                    return verified
            return Ok(payload)
        except Exception as error:
            return Err(str(error))

    def locked[T](self, work: Callable[[], Result[T]]) -> Result[T]:
        descriptor: int | None = None
        result: Result[T] = Err("Writer lock was not acquired")
        try:
            with self._parent(self.root / "writer.lock") as (directory, name):
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                identity = os.fstat(descriptor)
                try:
                    result = work()
                except Exception as error:
                    result = Err(str(error))
                finally:
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                        raise ValueError("Writer lock identity changed; refusing cleanup")
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
        except Exception as error:
            result = Err(str(error))
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception as error:
                    result = Err(str(error))
        return result
