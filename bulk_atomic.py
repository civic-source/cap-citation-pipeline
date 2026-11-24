"""Owned pointer publication and verified rollback after durability faults."""
from contextlib import ExitStack, contextmanager
import fcntl
import os
import stat
from uuid import uuid4


def _identity(info):
    return info.st_dev, info.st_ino


def discard_owned(directory, name, identity):
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(current) != _identity(identity):
        raise ValueError("Refusing cleanup of a foreign staging inode")
    os.unlink(name, dir_fd=directory)


@contextmanager
def selection_lock(directory, enabled=True):
    if not enabled:
        yield
        return
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise ValueError("Another writer owns pointer publication") from error
    try:
        yield
    finally:
        try:
            fcntl.flock(directory, fcntl.LOCK_UN)
        except OSError as error:
            raise OSError("selection outcome unknown: pointer lock release failed") from error


def _pointer(directory, name, limit):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Pointer must be a bounded single-link regular file")
        raw = source.read(limit + 1)
        current = os.fstat(source.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        if len(raw) > limit or any(getattr(info, key) != getattr(current, key) for key in fields):
            raise ValueError("Pointer changed during verification")
        if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(info):
            raise ValueError("Pointer path changed during verification")
        return info, raw



@contextmanager
def _pin(address, expected):
    if expected is None:
        yield
        return
    directory, name = address
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if _identity(os.fstat(descriptor)) != _identity(expected[0]):
            raise ValueError("Pointer inode changed before it could be pinned")
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise OSError("selection outcome unknown: pointer inode pin release failed") from error


def _backup(directory, previous):
    name = ".selection-" + uuid4().hex
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(previous)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)
        return name, identity
    except Exception:
        discard_owned(directory, name, identity)
        raise


def _matches(current, expected):
    if current is None or expected is None:
        return current is expected
    return _identity(current[0]) == _identity(expected[0]) and current[1] == expected[1]


def _rollback(staged, destination, prior, candidate, backup):
    source, _ = staged
    target, name = destination
    current = _pointer(target, name, max(len(candidate[1]), len(prior[1]) if prior else 0))
    if _matches(current, prior):
        return
    if not _matches(current, candidate):
        raise ValueError("Foreign pointer replaced this transaction's selection")
    if prior is None:
        os.unlink(name, dir_fd=target)
        restored = None
    else:
        retained = _pointer(source, backup[0], len(prior[1]))
        if retained is None or _identity(retained[0]) != _identity(backup[1]) or retained[1] != prior[1]:
            raise ValueError("Private rollback bytes or inode changed")
        os.replace(backup[0], name, src_dir_fd=source, dst_dir_fd=target)
        restored = retained
    os.fsync(target)
    os.fsync(source)
    if not _matches(_pointer(target, name, len(prior[1]) if prior else 0), restored):
        raise ValueError("Restored pointer identity or bytes could not be verified")



def retain_manifest(staged, destination, previous, content):
    if previous is not None:
        if previous != content:
            raise ValueError("Refusing immutable manifest replacement")
        return False
    source, name = staged
    target, selected = destination
    os.link(name, selected, src_dir_fd=source, dst_dir_fd=target, follow_symlinks=False)
    os.unlink(name, dir_fd=source)
    return True


def replace_pointer(staged, destination, previous, content):
    source, name = staged
    target, selected = destination
    candidate = _pointer(source, name, len(content))
    prior = _pointer(target, selected, len(previous) if previous is not None else 0)
    if candidate is None or candidate[1] != content or (prior[1] if prior else None) != previous:
        raise ValueError("Pointer publication identities changed before commit")
    with ExitStack() as pins:
        pins.enter_context(_pin(staged, candidate))
        pins.enter_context(_pin(destination, prior))
        backup = _backup(source, previous) if previous is not None else None
        try:
            if backup is not None:
                pins.enter_context(_pin((source, backup[0]), (backup[1], previous)))
            try:
                if not _matches(_pointer(target, selected, len(previous) if previous else 0), prior):
                    raise ValueError("Pointer changed before publication")
                os.replace(name, selected, src_dir_fd=source, dst_dir_fd=target)
                os.fsync(target)
                os.fsync(source)
                if not _matches(_pointer(target, selected, len(content)), candidate):
                    raise ValueError("Published pointer changed before acknowledgement")
            except Exception as failure:
                try:
                    _rollback(staged, destination, prior, candidate, backup)
                except Exception as recovery:
                    raise OSError(f"selection outcome unknown: {failure}; recovery: {recovery}") from failure
                raise
        finally:
            if backup is not None:
                try:
                    discard_owned(source, backup[0], backup[1])
                    os.fsync(source)
                except Exception as error:
                    raise OSError("selection outcome unknown: rollback staging cleanup failed") from error
