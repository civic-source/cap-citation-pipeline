"""Own a resource-bounded, offline native-reader subprocess for joined sources."""
import hashlib
import os
from pathlib import Path
import resource
import sys
import time

from bulk_common import Blob, Err, Ok, canonical_bytes, load_json
from bulk_github_transport import run_command

ROOT = Path(__file__).resolve().parent
DEFAULT_LIMITS = {"address_bytes": 4 * 1024**3, "cpu_seconds": 1800, "wall_seconds": 2400,
                  "file_bytes": 1024**3, "output_bytes": 1024**3, "partition_bytes": 32 * 1024**2,
                  "index_bytes": 512 * 1024**2, "batch_rows": 64, "max_rows": 300000,
                  "footer_bytes": 1024**2, "min_free_bytes": 4 * 1024**3}


def limits_value(supplied):
    if supplied is not None and (not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_LIMITS)):
        raise ValueError("Unknown worker limit")
    limits = {**DEFAULT_LIMITS, **(supplied or {})}
    reserve = limits.pop("min_free_bytes")
    if type(reserve) is not int or not 4 * 1024**3 <= reserve <= 1024**4:
        raise ValueError("Worker reserve must be at least 4 GiB")
    if any(type(value) is not int or not 0 < value <= DEFAULT_LIMITS[key] for key, value in limits.items()):
        raise ValueError("Worker limits must be positive bounded integers")
    return {**limits, "min_free_bytes": reserve}


def project_file(source: Path, blob: Blob, configuration: dict, output: Path, *, limits=None):
    try:
        bounds = limits_value(limits)
        if not 0 < blob.byte_length <= 1024**3:
            raise ValueError("Joined source exceeds 1 GiB reader bound")
        from joined_rules import configuration as validate_configuration
        validate_configuration(configuration)
        destination = Path(output).absolute()
        destination.mkdir(mode=0o700)
        job = {"source": str(Path(source).absolute()), "blob": blob.to_dict(), "configuration": configuration,
               "output": str(destination), "limits": bounds}
        path = destination / "job.json"
        path.write_bytes(canonical_bytes(job))
        result = run_command([sys.executable, "-s", str(ROOT / "joined_worker.py"), str(path)],
                             max_bytes=4 * 1024**2, timeout=bounds["wall_seconds"])
        path.unlink()
        if result.exit_code != 0:
            return Err(f"Bounded joined worker failed (exit {result.exit_code}): " + result.stderr[-4096:])
        value = load_json(result.stdout)
        if not isinstance(value, dict) or value.get("schema_version") != "cap.joined-evidence/v1":
            return Err("Invalid joined worker result")
        return Ok(value)
    except Exception as error:
        return Err(str(error))


def apply_limits(bounds, output):
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_IO_THREADS"):
        os.environ[name] = "1"
    os.environ["ARROW_DEFAULT_MEMORY_POOL"] = "system"
    os.environ["SQLITE_TMPDIR"] = output
    resource.setrlimit(resource.RLIMIT_AS, (bounds["address_bytes"], bounds["address_bytes"]))
    resource.setrlimit(resource.RLIMIT_CPU, (bounds["cpu_seconds"], bounds["cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_FSIZE, (bounds["file_bytes"], bounds["file_bytes"]))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def verify_source(path, blob, footer_limit):
    import stat
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != blob.byte_length:
            raise ValueError("Source must be exact regular unlinked artifact")
        digest = hashlib.sha256()
        while data := stream.read(65536):
            digest.update(data)
        if digest.hexdigest() != blob.sha256:
            raise ValueError("Source hash mismatch")
        stream.seek(-8, os.SEEK_END)
        tail = stream.read(8)
        if tail[4:] != b"PAR1" or not 0 < int.from_bytes(tail[:4], "little") <= footer_limit:
            raise ValueError("Invalid or oversized Parquet footer")


def decode(job):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from joined_output import Output
    from joined_projection import Projection
    from joined_rules import configuration
    if pa.__version__ != "23.0.1":
        raise ValueError("Joined reader requires pyarrow==23.0.1")
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    limits, directory = job["limits"], Path(job["output"])
    blob = Blob(**job["blob"])
    verify_source(job["source"], blob, limits["footer_bytes"])
    settings, grammar = configuration(job["configuration"])
    source = pq.ParquetFile(job["source"], thrift_string_size_limit=limits["footer_bytes"], thrift_container_size_limit=100000)
    if not 0 < source.metadata.num_rows <= limits["max_rows"] or source.num_row_groups > 64:
        raise ValueError("Joined source exceeds row/group budget")
    output = Output(directory, limits)
    projection = Projection(settings, grammar, output)
    try:
        scan_groups(source, projection, limits)
        output.flush()
        return {"schema_version": "cap.joined-evidence/v1", "partitions": output.partitions,
                "coverage": projection.coverage, "grammar": grammar, "span_unit": "unicode-codepoint",
                "text_encoding": "utf-8", "schema_sha256": hashlib.sha256(source.schema_arrow.serialize().to_pybytes()).hexdigest(),
                "pyarrow_version": pa.__version__}
    finally:
        output.close()


def scan_groups(source, projection, limits):
    columns = ["id", "case_name", "date_filed", "date_filed_is_approximate", "court_type", "court_jurisdiction", "court_full_name", "court_short_name", "citations", "opinions"]
    if not set(columns) <= set(source.schema_arrow.names):
        raise ValueError("Joined source schema is missing required fields")
    absolute = 0
    for group in range(source.num_row_groups):
        within = 0
        for batch in source.iter_batches(batch_size=limits["batch_rows"], row_groups=[group], columns=columns, use_threads=False):
            for row in batch.to_pylist():
                projection.record(row, {"row_group": group, "row_in_group": within, "source_row": absolute})
                within, absolute = within + 1, absolute + 1
            projection.output.db.commit()
            projection.output.check_index()
        if within != source.metadata.row_group(group).num_rows:
            raise ValueError("Incomplete joined row group")
    projection.coverage["row_groups_scanned"] = source.num_row_groups
    if absolute != source.metadata.num_rows:
        raise ValueError("Incomplete joined source enumeration")


def main():
    try:
        raw = Path(sys.argv[1]).read_bytes()
        if len(raw) > 4 * 1024**2:
            raise ValueError("Oversized worker job")
        job = load_json(raw)
        job["limits"] = limits_value(job["limits"])
        apply_limits(job["limits"], job["output"])
        started = time.monotonic()
        product = decode(job)
        observations = {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                        "elapsed_seconds": time.monotonic() - started}
        Path(job["output"], "resources.json").write_bytes(canonical_bytes(observations))
        sys.stdout.buffer.write(canonical_bytes(product))
        return 0
    except Exception as error:
        print(type(error).__name__ + ": " + str(error)[:2000], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
