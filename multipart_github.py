"""Write-once indexed raw-part releases with complete bounded readback."""
from dataclasses import dataclass
from pathlib import Path
import re
import os
import tempfile

from bulk_archive import archive_digest, regular_file
from bulk_common import Blob, Err, Ok, canonical_bytes, load_json
from bulk_github import (
    ReleaseLimits, github_command, lookup_release, release_metadata,
    validate_repository, verify_tag,
)
from bulk_github_transport import download_asset, run_gh
from multipart_archive import export_joined, import_joined, inspect_joined
from multipart_manifest import INDEX_BYTES, PART_BYTES, part_inventory, part_name

_RECEIPT = "multipart-receipt.json"
_LIMITS = ReleaseLimits(66, PART_BYTES)


@dataclass(frozen=True)
class _Context:
    store: object
    repository: str
    runner: object
    downloader: object


def _checked(result):
    if isinstance(result, Err):
        raise ValueError(result.error)
    return result.value


def replay_snapshot(store, payload):
    from joined_pipeline import replay_joined_snapshot
    return replay_joined_snapshot(store, payload)


def verify_producer(payload):
    from joined_pipeline import verify_joined_producer
    return verify_joined_producer(payload)


def _hex(value, length):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value):
        raise ValueError("Invalid multipart GitHub identity")
    return value


def _tag(snapshot, index):
    return f"precedent-joined-{_hex(snapshot, 64)[:16]}-{_hex(index, 64)[:16]}"


def _metadata(context, tag):
    return release_metadata(context.repository, tag, context.runner, limits=_LIMITS)


def _receipt(value, repository, tag):
    fields = {"schema_version", "snapshot_id", "repository", "tag", "index", "index_name", "producer_revision", "release_url"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != "cap.github-multipart/v1":
        raise ValueError("Invalid multipart GitHub receipt")
    if not isinstance(value["index"], dict) or set(value["index"]) != {"sha256", "byte_length"}:
        raise ValueError("Invalid multipart index receipt")
    blob = Blob(**value["index"])
    if not 0 < blob.byte_length <= INDEX_BYTES or value["index_name"] != f"joined-index-{blob.sha256}.json":
        raise ValueError("Invalid multipart index asset identity")
    if value["repository"] != repository or value["tag"] != tag or _tag(value["snapshot_id"], blob.sha256) != tag:
        raise ValueError("Multipart GitHub receipt scope mismatch")
    _hex(value["producer_revision"], 40)
    if value["release_url"] != f"https://github.com/{repository}/releases/tag/{tag}":
        raise ValueError("Multipart release URL mismatch")
    return value


def _assets(metadata):
    fields = ("id", "name", "size", "state", "url")
    return {asset["name"]: {key: asset[key] for key in fields} for asset in metadata["assets"]}


def _exact_assets(metadata, expected, complete=True):
    assets = _assets(metadata)
    if set(assets) - set(expected) or (complete and set(assets) != set(expected)):
        raise ValueError("Unexpected or missing multipart GitHub assets")
    if any(asset["size"] != expected[name].byte_length for name, asset in assets.items()):
        raise ValueError("Multipart GitHub asset length differs from index")
    return assets


def _download(context, asset, path, reference):
    if asset["size"] != reference.byte_length:
        raise ValueError("Multipart advertised asset length mismatch")
    _checked(context.store.ensure_capacity(reference.byte_length))
    _checked(context.downloader(asset, path))
    if _checked(archive_digest(path)) != reference:
        raise ValueError("Multipart GitHub readback hash or length mismatch")


def _load_receipt(context, metadata, directory):
    asset = _assets(metadata).get(_RECEIPT)
    if asset is None or asset["size"] > 65536:
        raise ValueError("Missing or oversized multipart receipt")
    path = directory / _RECEIPT
    _checked(context.store.ensure_capacity(asset["size"]))
    _checked(context.downloader(asset, path))
    with regular_file(path, 65536) as source:
        raw = source.read(65537)
    if len(raw) != asset["size"]:
        raise ValueError("Multipart receipt byte length mismatch")
    value = load_json(raw)
    if canonical_bytes(value) != raw:
        raise ValueError("Multipart receipt is not canonical")
    return _receipt(value, context.repository, metadata["tag_name"])


def _expected(receipt, index):
    result = {receipt["index_name"]: Blob(**receipt["index"]), _RECEIPT: Blob.of(canonical_bytes(receipt))}
    result.update({part_name(ref): ref for ref in part_inventory(index)})
    return result


def _bound_index(receipt, index):
    snapshot = index["snapshot"]
    producer = snapshot["payload"].get("producer", {}).get("revision")
    if snapshot["id"] != receipt["snapshot_id"] or producer != receipt["producer_revision"]:
        raise ValueError("Multipart index differs from receipt or producer")


def _readback(context, tag, directory, *, retain_parts=False):
    metadata = _metadata(context, tag)
    receipt = _load_receipt(context, metadata, directory)
    verify_tag(context.repository, tag, receipt["producer_revision"], context.runner)
    payload = directory / "payload"
    payload.mkdir()
    index_path = payload / receipt["index_name"]
    asset = _assets(metadata).get(index_path.name)
    if asset is None:
        raise ValueError("Missing multipart index asset")
    _download(context, asset, index_path, Blob(**receipt["index"]))
    index = _checked(inspect_joined(index_path))
    _bound_index(receipt, index)
    expected = _expected(receipt, index)
    assets = _exact_assets(metadata, expected)
    lengths = [ref.byte_length for ref in part_inventory(index)]
    _checked(context.store.ensure_capacity(sum(lengths) if retain_parts else max(lengths, default=0)))
    for ref in part_inventory(index):
        path = payload / part_name(ref)
        _download(context, assets[path.name], path, ref)
        if not retain_parts:
            path.unlink()
    final = _metadata(context, tag)
    if _exact_assets(final, expected) != assets:
        raise ValueError("Multipart asset set changed during readback")
    verify_tag(context.repository, tag, receipt["producer_revision"], context.runner)
    return receipt, index_path, assets


def _plan(context, identifier, directory):
    payload = _checked(context.store.load("joined-snapshot", identifier))
    _checked(replay_snapshot(context.store, payload))
    producer = _checked(verify_producer(payload))
    _hex(producer, 40)
    if payload.get("producer", {}).get("revision") != producer:
        raise ValueError("Verified producer differs from joined manifest")
    options = {}
    if os.path.lexists(context.store.root / "current-inventory.json"):
        from bulk_pipeline import replay_inventory
        inventory = _checked(context.store.load("inventory"))
        _checked(replay_inventory(context.store, inventory))
        options["inventory_id"] = Blob.of(canonical_bytes(inventory)).sha256
    exported = _checked(export_joined(context.store, identifier, directory / "export", **options))
    index = _checked(inspect_joined(Path(exported["index_path"])))
    reference = Blob(**exported["index"])
    tag = _tag(identifier, reference.sha256)
    receipt = {"schema_version": "cap.github-multipart/v1", "snapshot_id": identifier,
               "repository": context.repository, "tag": tag, "index": reference.to_dict(),
               "index_name": Path(exported["index_path"]).name, "producer_revision": producer,
               "release_url": f"https://github.com/{context.repository}/releases/tag/{tag}"}
    _receipt(receipt, context.repository, tag)
    _bound_index(receipt, index)
    receipt_path = directory / _RECEIPT
    _checked(context.store.ensure_capacity(len(canonical_bytes(receipt))))
    receipt_path.write_bytes(canonical_bytes(receipt))
    paths = {path.name: path for path in Path(exported["index_path"]).parent.iterdir()}
    paths[_RECEIPT] = receipt_path
    return receipt, _expected(receipt, index), paths


def _draft(context, receipt):
    tag, producer = receipt["tag"], receipt["producer_revision"]
    existing = lookup_release(context.repository, tag, context.runner)
    verify_tag(context.repository, tag, producer, context.runner, create=existing is None)
    if existing is None:
        github_command(context.runner, ["release", "create", tag, "--repo", context.repository,
                       "--target", producer, "--title", "Joined precedent " + receipt["snapshot_id"],
                       "--notes", "Verified historical joined cohort and candidate evidence.", "--draft", "--latest=false"])
    return _metadata(context, tag)


def _resume(context, receipt, expected, paths, directory):
    metadata = _draft(context, receipt)
    assets = _exact_assets(metadata, expected, complete=not metadata["draft"])
    verify = directory / "verify-existing"
    verify.mkdir()
    for name, asset in assets.items():
        path = verify / name
        _download(context, asset, path, expected[name])
        path.unlink()
    for name in sorted(set(expected) - set(assets)):
        github_command(context.runner, ["release", "upload", receipt["tag"], str(paths[name]), "--repo", context.repository])


def _publish(context, receipt, expected, verified_assets):
    tag = receipt["tag"]
    metadata = _metadata(context, tag)
    if _exact_assets(metadata, expected) != verified_assets:
        raise ValueError("Multipart assets changed after verified readback")
    verify_tag(context.repository, tag, receipt["producer_revision"], context.runner)
    if metadata["draft"]:
        github_command(context.runner, ["release", "edit", tag, "--repo", context.repository, "--draft=false", "--latest=false"])
    final = _metadata(context, tag)
    if _exact_assets(final, expected) != verified_assets:
        raise ValueError("Multipart assets changed during publication")
    if final["draft"]:
        raise ValueError("Multipart archive remains unpublished")
    verify_tag(context.repository, tag, receipt["producer_revision"], context.runner)
    return final.get("immutable")


def archive_joined_to_github(store, snapshot_id, repository, *, runner=run_gh, downloader=download_asset):
    try:
        context = _Context(store, validate_repository(repository), runner, downloader)
        with tempfile.TemporaryDirectory(prefix="joined-github-", dir=store.staging) as temporary:
            directory = Path(temporary)
            receipt, expected, paths = _plan(context, snapshot_id, directory)
            _resume(context, receipt, expected, paths, directory)
            readback = directory / "readback"
            readback.mkdir()
            actual, _, verified_assets = _readback(context, receipt["tag"], readback)
            if actual != receipt:
                raise ValueError("Remote multipart receipt differs from immutable plan")
            return Ok({**receipt, "serverImmutable": _publish(context, receipt, expected, verified_assets)})
    except Exception as error:
        return Err(str(error))


def restore_joined_from_github(store, repository, tag, *, runner=run_gh, downloader=download_asset):
    try:
        context = _Context(store, validate_repository(repository), runner, downloader)
        if not isinstance(tag, str) or not re.fullmatch(r"precedent-joined-[a-f0-9]{16}-[a-f0-9]{16}", tag):
            raise ValueError("Invalid joined archive tag")
        with tempfile.TemporaryDirectory(prefix="joined-github-restore-", dir=store.staging) as temporary:
            receipt, index_path, _ = _readback(context, tag, Path(temporary), retain_parts=True)
            return import_joined(store, index_path, expected_snapshot_id=receipt["snapshot_id"],
                                 expected_producer_revision=receipt["producer_revision"])
    except Exception as error:
        return Err(str(error))
