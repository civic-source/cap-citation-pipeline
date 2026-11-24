"""Multipart releases bind exact raw assets before publication or selection."""
import json
from pathlib import Path

import pytest

from bulk_common import Err, Ok
from bulk_github_transport import CommandResult
from bulk_store import Store
import multipart_github as github
from multipart_archive import export_joined, import_joined
from test_multipart_archive import accepted, replay, snapshot

REPO = "civic-source/cap-citation-pipeline"


class Remote:
    def __init__(self):
        self.releases, self.refs, self.contents, self.commands = {}, {}, {}, []
        self.corrupt = False
        self.fail_upload = None
        self.serial_readback = False

    def add(self, release, path):
        raw = Path(path).read_bytes()
        assert not any(a["name"] == Path(path).name for a in release["assets"])
        identifier = len(self.contents) + 1
        self.contents[identifier] = raw
        release["assets"].append({"id": identifier, "name": Path(path).name, "size": len(raw), "state": "uploaded", "url": f"https://api.github.com/repos/{REPO}/releases/assets/{identifier}"})

    def api(self, args):
        good = lambda value=None: CommandResult(0, "" if value is None else json.dumps(value), "")
        missing = lambda: CommandResult(1, "", "HTTP 404 Not Found")
        if args[0] == "api":
            endpoint = args[1]
            if endpoint.endswith("/git/refs"):
                ref = next(a[4:] for a in args if a.startswith("ref="))
                self.refs[ref] = next(a[4:] for a in args if a.startswith("sha="))
                return good()
            if "/git/ref/tags/" in endpoint:
                ref = "refs/tags/" + endpoint.rsplit("/", 1)[1]
                return good({"ref": ref, "object": {"sha": self.refs[ref], "type": "commit"}}) if ref in self.refs else missing()
            if "/releases?" in endpoint:
                return good(list(self.releases.values()))
            if "/releases/tags/" in endpoint:
                value = self.releases.get(endpoint.rsplit("/", 1)[1])
                return good(value) if value and not value["draft"] else missing()
            if "/assets?" in endpoint:
                from urllib.parse import parse_qs, urlsplit
                page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
                release = next(r for r in self.releases.values() if str(r["id"]) == endpoint.split("/releases/")[1].split("/")[0])
                return good(release["assets"][(page-1)*32:page*32])
            if "/releases/" in endpoint:
                value = next((r for r in self.releases.values() if str(r["id"]) == endpoint.rsplit("/", 1)[1]), None)
                return good(value) if value else missing()

    def runner(self, args):
        self.commands.append(args)
        if args[0] == "api":
            return self.api(args)
        good = lambda value=None: CommandResult(0, "" if value is None else json.dumps(value), "")
        if args[:2] == ["release", "create"]:
            tag = args[2]
            assert tag not in self.releases
            self.releases[tag] = {"id": len(self.releases)+1, "tag_name": tag, "draft": True, "immutable": False, "assets": []}
            return good()
        if args[:2] == ["release", "upload"]:
            path = Path(args[3])
            if self.fail_upload == path.name:
                return CommandResult(1, "", "upload interrupted")
            self.add(self.releases[args[2]], path)
            return good()
        if args[:2] == ["release", "edit"]:
            self.releases[args[2]]["draft"] = False
            return good()
        raise AssertionError(args)

    def downloader(self, asset, path):
        self.commands.append(["download", asset["name"]])
        if self.serial_readback and path.name.startswith("part-") and "readback" in path.parts:
            assert not list(path.parent.glob("part-*"))
        raw = self.contents[asset["id"]]
        path.write_bytes(raw + (b"tamper" if self.corrupt and asset["name"].startswith("part-") else b""))
        return Ok(None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "original", min_free_bytes=0)
    identifier, payload = snapshot(store)
    payload["producer"]["revision"] = "0"*40
    from bulk_archive import references
    identifier = accepted(store.accept("joined-snapshot", payload, list(references(payload))))
    monkeypatch.setattr(github, "verify_producer", lambda payload: Ok("0"*40))
    monkeypatch.setattr(github, "replay_snapshot", replay)
    monkeypatch.setattr(github, "export_joined", lambda s, i, p: export_joined(s, i, p, part_size=8))
    monkeypatch.setattr(github, "import_joined", lambda s, p, **kwargs: import_joined(s, p, replayer=replay, **kwargs))
    return store, identifier, Remote()


def test_publish_after_serial_readback_and_restore_exact_snapshot(setup, tmp_path):
    store, identifier, remote = setup
    remote.serial_readback = True
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, runner=remote.runner, downloader=remote.downloader))
    assert receipt["serverImmutable"] is False
    assert receipt["tag"].startswith("precedent-joined-" + identifier[:16])
    before_publish = next(i for i,a in enumerate(remote.commands) if a[:2] == ["release", "edit"])
    assert all(i < before_publish for i,a in enumerate(remote.commands) if a[0] == "download")
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    accepted(restored.init())
    assert accepted(github.restore_joined_from_github(restored, REPO, receipt["tag"], runner=remote.runner, downloader=remote.downloader)) == identifier
    assert accepted(restored.load("joined-snapshot")) == accepted(store.load("joined-snapshot"))


def test_corrupt_readback_stays_draft_then_resumes_without_clobber(setup):
    store, identifier, remote = setup
    remote.corrupt = True
    options = {"runner": remote.runner, "downloader": remote.downloader}
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Err)
    assert all(r["draft"] for r in remote.releases.values())
    assets = dict(remote.contents)
    remote.corrupt = False
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Ok)
    assert remote.contents == assets
    assert len([a for a in remote.commands if a[:2] == ["release", "create"]]) == 1


@pytest.mark.parametrize("mutation", ["extra", "foreign-url", "wrong-size", "moved-tag", "wrong-bytes"])
def test_bad_remote_identity_never_advances_restore_pointer(setup, tmp_path, mutation):
    store, identifier, remote = setup
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, **options))
    release = remote.releases[receipt["tag"]]
    asset = next(a for a in release["assets"] if a["name"].startswith("part-"))
    if mutation == "extra":
        path = tmp_path / "unexpected.bin"
        path.write_bytes(b"x")
        remote.add(release, path)
    elif mutation == "foreign-url":
        asset["url"] = "https://example.invalid/asset"
    elif mutation == "wrong-size":
        asset["size"] += 1
    elif mutation == "moved-tag":
        remote.refs["refs/tags/"+receipt["tag"]] = "a"*40
    else:
        remote.contents[asset["id"]] = b"x"*asset["size"]
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    accepted(restored.init())
    assert isinstance(github.restore_joined_from_github(restored, REPO, receipt["tag"], **options), Err)
    assert not (restored.root / "current-joined-snapshot.json").exists()


def test_published_release_is_idempotent_without_upload_or_republish(setup):
    store, identifier, remote = setup
    options = {"runner": remote.runner, "downloader": remote.downloader}
    first = accepted(github.archive_joined_to_github(store, identifier, REPO, **options))
    count = len(remote.commands)
    assert accepted(github.archive_joined_to_github(store, identifier, REPO, **options)) == first
    assert not any(a[:2] in [["release", "upload"], ["release", "edit"]] for a in remote.commands[count:])


def test_download_counter_changes_do_not_invalidate_verified_asset_identity(setup):
    store, identifier, remote = setup
    original = remote.downloader
    def downloader(asset, path):
        result = original(asset, path)
        for release in remote.releases.values():
            for item in release["assets"]:
                if item["id"] == asset["id"]:
                    item["download_count"] = item.get("download_count", 0) + 1
        return result
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, runner=remote.runner, downloader=downloader), Ok)


def test_more_than_32_assets_use_actual_paginated_inventory(setup):
    store, identifier, remote = setup
    identifier, payload = snapshot(store, source=bytes(range(256)) + bytes(range(64)))
    payload["producer"]["revision"] = "0"*40
    from bulk_archive import references
    identifier = accepted(store.accept("joined-snapshot", payload, list(references(payload))))
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, runner=remote.runner, downloader=remote.downloader))
    assert len(remote.releases[receipt["tag"]]["assets"]) > 32
    assert any("assets?per_page=32&page=2" in a[1] for a in remote.commands if a[0] == "api")


def test_published_missing_asset_is_never_uploaded_or_republished(setup):
    store, identifier, remote = setup
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, **options))
    remote.releases[receipt["tag"]]["assets"].pop()
    count = len(remote.commands)
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Err)
    assert not any(a[:2] in [["release", "upload"], ["release", "edit"]] for a in remote.commands[count:])


def test_draft_missing_parts_resume_only_after_existing_assets_verify(setup):
    store, identifier, remote = setup
    stopped = []
    def interrupted(args):
        if args[:2] == ["release", "upload"] and Path(args[3]).name.startswith("part-") and not stopped:
            stopped.append(True)
            return CommandResult(1, "", "interrupted")
        return remote.runner(args)
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, runner=interrupted, downloader=remote.downloader), Err)
    before = dict(remote.contents)
    assert all(r["draft"] for r in remote.releases.values())
    result = github.archive_joined_to_github(store, identifier, REPO, runner=remote.runner, downloader=remote.downloader)
    assert isinstance(result, Ok), result
    assert all(remote.contents[key] == raw for key, raw in before.items())
    assert len([a for a in remote.commands if a[:2] == ["release", "create"]]) == 1


def test_all_64_distinct_parts_plus_index_and_receipt_are_supported(setup):
    store, _, remote = setup
    identifier, payload = snapshot(store, source=b"".join(i.to_bytes(8, "big") for i in range(61)))
    payload["producer"]["revision"] = "0"*40
    from bulk_archive import references
    identifier = accepted(store.accept("joined-snapshot", payload, list(references(payload))))
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, runner=remote.runner, downloader=remote.downloader))
    assert len(remote.releases[receipt["tag"]]["assets"]) == 66
    assert any("assets?per_page=32&page=3" in a[1] for a in remote.commands if a[0] == "api")


def test_receipt_size_and_invalid_repository_refused_before_any_download(setup, tmp_path):
    store, identifier, remote = setup
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, **options))
    asset = next(a for a in remote.releases[receipt["tag"]]["assets"] if a["name"] == "multipart-receipt.json")
    asset["size"] = 65537
    before = len([a for a in remote.commands if a[0] == "download"])
    assert isinstance(github.restore_joined_from_github(store, REPO, receipt["tag"], **options), Err)
    assert isinstance(github.restore_joined_from_github(store, "../wrong", receipt["tag"], **options), Err)
    assert len([a for a in remote.commands if a[0] == "download"]) == before


def test_restore_quota_refusal_never_changes_existing_pointer(setup, tmp_path):
    store, identifier, remote = setup
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, **options))
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    prior, _ = snapshot(restored, source=b"prior source bytes", evidence=b"prior")
    before = (restored.root / "current-joined-snapshot.json").read_bytes()
    restored._quota = 1
    assert isinstance(github.restore_joined_from_github(restored, REPO, receipt["tag"], **options), Err)
    assert (restored.root / "current-joined-snapshot.json").read_bytes() == before


def test_semantic_replay_failure_prevents_all_remote_mutations(setup, monkeypatch):
    store, identifier, remote = setup
    monkeypatch.setattr(github, "replay_snapshot", lambda *args: Err("product cannot be reproduced"), raising=False)
    result = github.archive_joined_to_github(store, identifier, REPO, runner=remote.runner, downloader=remote.downloader)
    assert isinstance(result, Err) and "reproduced" in result.error
    assert remote.commands == []
    assert remote.releases == {}


def test_changed_existing_draft_bytes_are_not_replaced(setup):
    store, identifier, remote = setup
    remote.corrupt = True
    options = {"runner": remote.runner, "downloader": remote.downloader}
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Err)
    remote.corrupt = False
    asset = next(a for r in remote.releases.values() for a in r["assets"] if a["name"].startswith("part-"))
    remote.contents[asset["id"]] = b"x" * asset["size"]
    before = len(remote.commands)
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Err)
    assert not any(a[:2] in [["release", "upload"], ["release", "edit"]] for a in remote.commands[before:])


def test_unverified_producer_or_corrupt_optional_inventory_prevents_remote_writes(setup, monkeypatch):
    store, identifier, remote = setup
    monkeypatch.setattr(github, "verify_producer", lambda payload: Err("commit recipe mismatch"))
    options = {"runner": remote.runner, "downloader": remote.downloader}
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Err)
    assert remote.commands == []
    monkeypatch.setattr(github, "verify_producer", lambda payload: Ok("0"*40))
    (store.root / "current-inventory.json").write_bytes(b"not-json")
    assert isinstance(github.archive_joined_to_github(store, identifier, REPO, **options), Err)
    assert remote.commands == []


def test_duplicate_asset_ids_are_rejected_before_restore_download(setup):
    store, identifier, remote = setup
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = accepted(github.archive_joined_to_github(store, identifier, REPO, **options))
    assets = remote.releases[receipt["tag"]]["assets"]
    assets[1]["id"] = assets[0]["id"]
    assets[1]["url"] = assets[0]["url"]
    before = len([a for a in remote.commands if a[0] == "download"])
    assert isinstance(github.restore_joined_from_github(store, REPO, receipt["tag"], **options), Err)
    assert len([a for a in remote.commands if a[0] == "download"]) == before
