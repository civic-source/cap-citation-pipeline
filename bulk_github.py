"""Write-once GitHub archives with bounded readback and producer-tag checks."""
from collections.abc import Callable
from dataclasses import dataclass
import re
from pathlib import Path
import tempfile

from bulk_archive import MAX_ARCHIVE, archive_digest, export_snapshot, import_snapshot, inspect_archive, regular_file
from bulk_common import Blob, Err, Ok, Result, canonical_bytes, load_json
from bulk_github_transport import CommandResult, download_asset, run_gh
from bulk_store import Store


@dataclass(frozen=True)
class _Context:
    store: Store
    repository: str
    runner: Callable[[list[str]], CommandResult]
    downloader: Callable[[dict, Path], Result[None]]


def _repository(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
        raise ValueError("Invalid GitHub repository identity")
    return value


def _command(runner, arguments):
    result = runner(arguments)
    if result.exit_code != 0:
        raise ValueError("GitHub archive command failed: " + result.stderr)
    return result.stdout


def _release_identity(value, tag, expected_id=None):
    if not isinstance(value, dict) or value.get("tag_name") != tag or type(value.get("id")) is not int or not 0 < value["id"] <= (1 << 53) - 1:
        raise ValueError("GitHub release identity mismatch")
    if expected_id is not None and value["id"] != expected_id:
        raise ValueError("GitHub draft release ID changed")
    return value


def _lookup(repository, tag, runner):
    direct = runner(["api", f"repos/{repository}/releases/tags/{tag}"])
    if direct.exit_code == 0:
        return _release_identity(load_json(direct.stdout), tag)
    if not re.search(r"HTTP 404|Not Found|release not found", direct.stderr, re.I):
        raise ValueError("GitHub release lookup failed: " + direct.stderr)
    for page in range(1, 11):
        raw = _command(runner, ["api", f"repos/{repository}/releases?per_page=100&page={page}"])
        values = load_json(raw)
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(item, dict) for item in values):
            raise ValueError("Malformed bounded GitHub release listing")
        matches = [item for item in values if item.get("tag_name") == tag]
        if len(matches) > 1:
            raise ValueError("Duplicate GitHub archive release tag")
        if matches:
            discovered = _release_identity(matches[0], tag)
            found = load_json(_command(runner, ["api", f"repos/{repository}/releases/{discovered['id']}"]))
            return _release_identity(found, tag, discovered["id"])
        if len(values) < 100:
            return None
    raise ValueError("GitHub release discovery exceeded its bounded history")


@dataclass(frozen=True)
class ReleaseLimits:
    max_assets: int = 32
    max_bytes: int = MAX_ARCHIVE


def _validate_release(value, repository, limits):
    if value is None or type(value.get("draft")) is not bool or not isinstance(value.get("assets"), list) or len(value["assets"]) > limits.max_assets:
        raise ValueError("Missing or malformed GitHub archive release")
    if value.get("immutable") is not None and type(value["immutable"]) is not bool:
        raise ValueError("Invalid GitHub release immutability flag")
    names, ids = set(), set()
    for asset in value["assets"]:
        if not isinstance(asset, dict) or type(asset.get("id")) is not int or not 0 < asset["id"] <= (1 << 53) - 1:
            raise ValueError("Invalid GitHub archive asset identity")
        if not isinstance(asset.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", asset["name"]):
            raise ValueError("Invalid GitHub archive asset name")
        if type(asset.get("size")) is not int or not 0 <= asset["size"] <= limits.max_bytes or asset.get("state") != "uploaded":
            raise ValueError("GitHub archive asset is oversized or incomplete")
        if asset.get("url") != f"https://api.github.com/repos/{repository}/releases/assets/{asset['id']}" or asset["name"] in names or asset["id"] in ids:
            raise ValueError("GitHub asset URL or uniqueness mismatch")
        names.add(asset["name"])
        ids.add(asset["id"])
    return value


def _metadata(repository, tag, runner):
    return _validate_release(_lookup(repository, tag, runner), repository, ReleaseLimits())


def release_metadata(repository, tag, runner, *, limits=ReleaseLimits()):
    """Enumerate the actual bounded asset set, including draft release pages."""
    if type(limits.max_assets) is not int or not 0 < limits.max_assets <= 66:
        raise ValueError("Invalid release asset count bound")
    if type(limits.max_bytes) is not int or not 0 <= limits.max_bytes <= MAX_ARCHIVE:
        raise ValueError("Invalid release asset byte bound")
    value = _lookup(repository, tag, runner)
    if value is None:
        raise ValueError("Missing GitHub archive release")
    assets = []
    for page in range(1, 4):
        raw = _command(runner, ["api", f"repos/{repository}/releases/{value['id']}/assets?per_page=32&page={page}"])
        batch = load_json(raw)
        if not isinstance(batch, list) or len(batch) > 32 or len(assets) + len(batch) > limits.max_assets:
            raise ValueError("GitHub release assets exceed bounded enumeration")
        assets.extend(batch)
        if len(batch) < 32:
            return _validate_release({**value, "assets": assets}, repository, limits)
    raise ValueError("GitHub asset enumeration exceeded page bound")


validate_repository = _repository
github_command = _command
lookup_release = _lookup


def _tag(repository, tag, revision, runner, create=False):
    endpoint = f"repos/{repository}/git/ref/tags/{tag}"
    response = runner(["api", endpoint])
    if response.exit_code != 0 and create and re.search(r"HTTP 404|Not Found", response.stderr, re.I):
        _command(runner, ["api", f"repos/{repository}/git/refs", "--method", "POST", "-f", "ref=refs/tags/" + tag, "-f", "sha=" + revision])
        response = runner(["api", endpoint])
    if response.exit_code != 0:
        raise ValueError("Cannot verify GitHub producer tag: " + response.stderr)
    reference = load_json(response.stdout)
    if not isinstance(reference, dict) or reference.get("ref") != "refs/tags/" + tag:
        raise ValueError("GitHub archive tag identity mismatch")
    object = reference.get("object")
    for depth in range(5):
        if not isinstance(object, dict) or not isinstance(object.get("sha"), str) or not re.fullmatch(r"[a-f0-9]{40}", object["sha"]):
            raise ValueError("Invalid GitHub producer Git object")
        if object.get("type") == "commit":
            if object["sha"] != revision:
                raise ValueError("GitHub archive tag does not retain the exact producer revision")
            return
        if object.get("type") != "tag" or depth == 4:
            raise ValueError("Unsupported or excessively nested producer tag")
        annotated = load_json(_command(runner, ["api", f"repos/{repository}/git/tags/{object['sha']}"]))
        if not isinstance(annotated, dict) or annotated.get("sha") != object["sha"]:
            raise ValueError("Annotated producer tag identity mismatch")
        object = annotated.get("object")
    raise ValueError("Unresolved producer tag")


def _tag_name(snapshot_id, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid snapshot or bundle identity")
    return f"precedent-data-{snapshot_id[:16]}-{digest[:16]}"


def _receipt(raw, repository, tag):
    value = load_json(raw)
    fields = {"schema_version", "snapshot_id", "repository", "tag", "asset_name", "blob", "producer_revision", "release_url"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != "cap.github-archive/v1":
        raise ValueError("Invalid GitHub archive receipt")
    if not isinstance(value["blob"], dict) or set(value["blob"]) != {"sha256", "byte_length"}:
        raise ValueError("Invalid GitHub archive blob receipt")
    blob = Blob(**value["blob"])
    if value["repository"] != repository or value["tag"] != tag or _tag_name(value["snapshot_id"], blob.sha256) != tag:
        raise ValueError("GitHub archive receipt scope mismatch")
    if value["asset_name"] != f"precedent-{value['snapshot_id']}-{blob.sha256}.tar.gz" or not re.fullmatch(r"[a-f0-9]{40}", value["producer_revision"]):
        raise ValueError("GitHub archive producer or asset mismatch")
    if value["release_url"] != f"https://github.com/{repository}/releases/tag/{tag}" or blob.byte_length > MAX_ARCHIVE:
        raise ValueError("GitHub archive URL or size mismatch")
    return value


def _download(context, metadata, name, path, max_bytes):
    store, downloader = context.store, context.downloader
    asset = next((item for item in metadata["assets"] if item["name"] == name), None)
    if asset is None or asset["size"] > max_bytes:
        raise ValueError("Missing or oversized GitHub archive asset")
    capacity = store.ensure_capacity(asset["size"])
    if isinstance(capacity, Err):
        raise ValueError(capacity.error)
    result = downloader(asset, path)
    if isinstance(result, Err):
        raise ValueError(result.error)
    actual = archive_digest(path)
    if isinstance(actual, Err) or actual.value.byte_length != asset["size"]:
        raise ValueError("Downloaded GitHub asset length mismatch")
    return asset


def _readback(context, tag, directory):
    repository, runner = context.repository, context.runner
    metadata = _metadata(repository, tag, runner)
    receipt_path = directory / "archive-receipt.json"
    _download(context, metadata, receipt_path.name, receipt_path, 65536)
    with regular_file(receipt_path, 65536) as source:
        receipt = _receipt(source.read(65537), repository, tag)
    _tag(repository, tag, receipt["producer_revision"], runner)
    path = directory / receipt["asset_name"]
    asset = _download(context, metadata, receipt["asset_name"], path, receipt["blob"]["byte_length"])
    if asset["size"] != receipt["blob"]["byte_length"]:
        raise ValueError("GitHub asset size differs from receipt")
    digest = archive_digest(path)
    if isinstance(digest, Err) or digest.value != Blob(**receipt["blob"]):
        raise ValueError("GitHub binary readback hash mismatch")
    bundle = inspect_archive(path)
    if isinstance(bundle, Err):
        raise ValueError(bundle.error)
    snapshot = bundle.value["snapshot"]
    if snapshot["id"] != receipt["snapshot_id"] or snapshot["payload"].get("producer", {}).get("revision") != receipt["producer_revision"]:
        raise ValueError("Archived manifest differs from GitHub receipt")
    return receipt, path


def _publish(repository, tag, runner):
    metadata = _metadata(repository, tag, runner)
    if metadata["draft"]:
        _command(runner, ["release", "edit", tag, "--repo", repository, "--draft=false", "--latest=false"])
    final = _metadata(repository, tag, runner)
    if final["draft"]:
        raise ValueError("GitHub archive remains unpublished")
    return final.get("immutable")


def archive_to_github(store: Store, snapshot_id: str, repository: str, *, runner=run_gh, downloader=download_asset) -> Result[dict]:
    try:
        _repository(repository)
        loaded = store.load("snapshot", snapshot_id)
        if isinstance(loaded, Err):
            return loaded
        producer = loaded.value.get("producer", {}).get("revision")
        if not isinstance(producer, str) or not re.fullmatch(r"[a-f0-9]{40}", producer):
            return Err("Commit the exact producer and recapture before GitHub archival")
        with tempfile.TemporaryDirectory(prefix="github-", dir=store.staging) as temporary:
            directory = Path(temporary)
            payload = directory / "payload.tar.gz"
            exported = export_snapshot(store, snapshot_id, payload)
            if isinstance(exported, Err):
                return exported
            tag = _tag_name(snapshot_id, exported.value.sha256)
            name = f"precedent-{snapshot_id}-{exported.value.sha256}.tar.gz"
            path = directory / name
            payload.rename(path)
            receipt = {"schema_version": "cap.github-archive/v1", "snapshot_id": snapshot_id, "repository": repository, "tag": tag,
                       "asset_name": name, "blob": exported.value.to_dict(), "producer_revision": producer,
                       "release_url": f"https://github.com/{repository}/releases/tag/{tag}"}
            receipt_path = directory / "archive-receipt.json"
            receipt_path.write_bytes(canonical_bytes(receipt))
            existing = _lookup(repository, tag, runner)
            if existing is None:
                _tag(repository, tag, producer, runner, True)
                _command(runner, ["release", "create", tag, str(path), str(receipt_path), "--repo", repository, "--target", producer,
                                  "--title", "Precedent data " + snapshot_id, "--notes", "Verified retained source and candidate evidence archive.", "--draft", "--latest=false"])
            readback = directory / "readback"
            readback.mkdir()
            verified, _ = _readback(_Context(store, repository, runner, downloader), tag, readback)
            if verified != receipt:
                raise ValueError("Existing archive differs from current immutable bundle")
            return Ok({**receipt, "serverImmutable": _publish(repository, tag, runner)})
    except Exception as error:
        return Err(str(error))


def restore_from_github(store: Store, repository: str, tag: str, *, runner=run_gh, downloader=download_asset) -> Result[str]:
    try:
        _repository(repository)
        if not isinstance(tag, str) or not re.fullmatch(r"precedent-data-[a-f0-9]{16}-[a-f0-9]{16}", tag):
            raise ValueError("Invalid precedent archive tag")
        with tempfile.TemporaryDirectory(prefix="github-restore-", dir=store.staging) as temporary:
            receipt, path = _readback(_Context(store, repository, runner, downloader), tag, Path(temporary))
            return import_snapshot(store, path, expected_snapshot_id=receipt["snapshot_id"], expected_producer_revision=receipt["producer_revision"])
    except Exception as error:
        return Err(str(error))


verify_tag = _tag
