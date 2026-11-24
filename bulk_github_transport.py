"""Bounded subprocess transport without exposing GitHub credentials."""
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from bulk_common import Err, Ok, Result


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


def _pump(child, max_bytes, timeout, sink):
    deadline, output, errors, size = time.monotonic() + timeout, [], b"", 0
    with selectors.DefaultSelector() as selector:
        for stream in (child.stdout, child.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("GitHub subprocess exceeded its time budget")
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.fileobj is child.stderr:
                    errors = (errors + chunk)[-8192:]
                else:
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("GitHub subprocess output exceeds its byte budget")
                    if sink is None:
                        output.append(chunk)
                    else:
                        sink(chunk)
        code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
    return code, b"".join(output), errors.decode("utf-8", errors="replace"), size


def _process(command, max_bytes, timeout, sink=None):
    if type(max_bytes) is not int or max_bytes < 0 or timeout <= 0:
        raise ValueError("Invalid subprocess byte or time budget")
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        return _pump(child, max_bytes, timeout, sink)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)
        raise
    finally:
        child.stdout.close()
        child.stderr.close()


def run_command(command: list[str], *, max_bytes=2 * 1024**2, timeout=120) -> CommandResult:
    try:
        code, output, errors, _ = _process(command, max_bytes, timeout)
        return CommandResult(code, output.decode("utf-8", errors="strict"), errors)
    except Exception as error:
        return CommandResult(1, "", str(error))


def run_gh(arguments: list[str]) -> CommandResult:
    return run_command(["gh", *arguments])


def bounded_download(command: list[str], destination: Path, expected_length: int, *, timeout=600) -> Result[None]:
    created, identity = False, None
    try:
        if type(expected_length) is not int or not 0 <= expected_length <= 256 * 1024**2:
            raise ValueError("Invalid GitHub binary byte budget")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created, identity = True, os.fstat(descriptor)
        with os.fdopen(descriptor, "wb") as output:
            code, _, errors, size = _process(command, expected_length, timeout, output.write)
            if code != 0 or size != expected_length:
                raise ValueError("GitHub binary download failed or length differs: " + errors)
            output.flush()
            os.fsync(output.fileno())
        return Ok(None)
    except Exception as error:
        if created:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    return Err("Download path identity changed; cleanup was refused")
                destination.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup:
                return Err(f"Download failed: {error}; cleanup failed: {cleanup}")
        return Err(str(error))


def download_asset(asset: dict, destination: Path) -> Result[None]:
    return bounded_download(["gh", "api", asset["url"], "-H", "Accept: application/octet-stream"], destination, asset["size"])
