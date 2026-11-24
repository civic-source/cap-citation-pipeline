"""Public GitHub transport is bounded before writing received bytes."""
import sys

from bulk_common import Err, Ok
from bulk_github_transport import bounded_download, run_command


def test_binary_transport_rejects_overflow_and_removes_partial_files(tmp_path):
    path = tmp_path / "overflow"
    result = bounded_download([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*100000)"], path, 100)
    assert isinstance(result, Err)
    assert not path.exists()


def test_binary_transport_checks_length_failure_and_existing_path(tmp_path):
    path = tmp_path / "artifact"
    assert isinstance(bounded_download([sys.executable, "-c", "print('x',end='')"], path, 2), Err)
    assert not path.exists()
    assert isinstance(bounded_download([sys.executable, "-c", "print('ok',end='')"], path, 2), Ok)
    assert isinstance(bounded_download([sys.executable, "-c", "print('xx',end='')"], path, 2), Err)
    assert path.read_bytes() == b"ok"


def test_metadata_transport_enforces_output_and_time_budgets():
    result = run_command([sys.executable, "-c", "print('x'*100000)"], max_bytes=10)
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "byte budget" in result.stderr
    assert run_command([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.05).exit_code != 0

from pathlib import Path
import json

from bulk_github import archive_to_github, restore_from_github
from bulk_github_transport import CommandResult
from bulk_store import Store
from test_bulk_archive import fixture, value


class Remote:
    def __init__(self):
        self.releases, self.refs, self.contents, self.commands = {}, {}, {}, []
        self.corrupt, self.invisible_drafts = False, True

    def runner(self, args):
        self.commands.append(list(args))
        good = lambda data=None: CommandResult(0, "" if data is None else json.dumps(data), "")
        missing = lambda: CommandResult(1, "", "HTTP 404 Not Found")
        if args[0] == "api":
            endpoint = args[1]
            if endpoint.endswith("/git/refs"):
                ref = next(arg[4:] for arg in args if arg.startswith("ref="))
                assert ref not in self.refs
                self.refs[ref] = next(arg[4:] for arg in args if arg.startswith("sha="))
                return good()
            if "/git/ref/tags/" in endpoint:
                ref = "refs/tags/" + endpoint.rsplit("/", 1)[1]
                return good({"ref": ref, "object": {"sha": self.refs[ref], "type": "commit"}}) if ref in self.refs else missing()
            if "/releases?" in endpoint:
                return good(list(self.releases.values()))
            if "/releases/tags/" in endpoint:
                release = self.releases.get(endpoint.rsplit("/", 1)[1])
                return good(release) if release and not (release["draft"] and self.invisible_drafts) else missing()
            if "/releases/" in endpoint:
                release = next((item for item in self.releases.values() if str(item["id"]) == endpoint.rsplit("/", 1)[1]), None)
                return good(release) if release else missing()
        if args[:2] == ["release", "create"]:
            tag = args[2]
            assert tag not in self.releases
            assets = []
            for path in args[3:5]:
                path = Path(path)
                id = len(self.contents) + 1
                raw = path.read_bytes()
                self.contents[id] = raw
                assets.append({"id": id, "name": path.name, "size": len(raw), "state": "uploaded", "url": f"https://api.github.com/repos/civic-source/cap-citation-pipeline/releases/assets/{id}"})
            self.releases[tag] = {"id": len(self.releases) + 1, "tag_name": tag, "draft": True, "immutable": False, "assets": assets}
            return good()
        if args[:2] == ["release", "edit"]:
            self.releases[args[2]]["draft"] = False
            return good()
        raise AssertionError(args)

    def downloader(self, asset, path):
        self.commands.append(["download", asset["name"]])
        raw = self.contents[asset["id"]]
        path.write_bytes(raw + (b"tampered" if self.corrupt and asset["name"].endswith(".gz") else b""))
        return Ok(None)


def test_publishes_after_readback_and_restores_exact_replayed_snapshot(tmp_path):
    store, snapshot_id, payload, _ = fixture(tmp_path)
    remote = Remote()
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options))
    assert receipt["serverImmutable"] is False
    assert remote.commands.index(next(item for item in remote.commands if item[0] == "download")) < remote.commands.index(next(item for item in remote.commands if item[:2] == ["release", "edit"]))
    assert value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options)) == receipt
    assert len([item for item in remote.commands if item[:2] == ["release", "create"]]) == 1
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    value(restored.init())
    assert value(restore_from_github(restored, "civic-source/cap-citation-pipeline", receipt["tag"], **options)) == snapshot_id
    assert value(restored.load("snapshot")) == payload


def test_failed_readback_stays_draft_and_resumes_without_overwriting_assets(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    remote = Remote()
    remote.corrupt = True
    options = {"runner": remote.runner, "downloader": remote.downloader}
    assert isinstance(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options), Err)
    assert all(item["draft"] for item in remote.releases.values())
    before = dict(remote.contents)
    remote.corrupt = False
    assert isinstance(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options), Ok)
    assert remote.contents == before
    assert len([item for item in remote.commands if item[:2] == ["release", "create"]]) == 1


def test_moved_tag_is_rejected_before_restored_pointer_acceptance(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    remote = Remote()
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options))
    remote.refs["refs/tags/" + receipt["tag"]] = "a" * 40
    restored = Store(tmp_path / "restored", min_free_bytes=0)
    value(restored.init())
    assert isinstance(restore_from_github(restored, "civic-source/cap-citation-pipeline", receipt["tag"], **options), Err)
    assert not (restored.root / "current-snapshot.json").exists()


def test_rejects_oversized_or_foreign_asset_metadata_before_download(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    remote = Remote()
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options))
    metadata = remote.releases[receipt["tag"]]
    original_calls = len([item for item in remote.commands if item[0] == "download"])
    asset = next(item for item in metadata["assets"] if item["name"] == "archive-receipt.json")
    original_size = asset["size"]
    asset["size"] = 65537
    assert isinstance(restore_from_github(store, "civic-source/cap-citation-pipeline", receipt["tag"], **options), Err)
    asset["size"], asset["url"] = original_size, "https://example.invalid/untrusted"
    assert isinstance(restore_from_github(store, "civic-source/cap-citation-pipeline", receipt["tag"], **options), Err)
    assert len([item for item in remote.commands if item[0] == "download"]) == original_calls


def test_peels_an_annotated_tag_to_the_exact_producer(tmp_path):
    store, snapshot_id, _, _ = fixture(tmp_path)
    remote = Remote()
    options = {"runner": remote.runner, "downloader": remote.downloader}
    receipt = value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options))

    def runner(args):
        if args[0] == "api" and "/git/ref/tags/" in args[1]:
            return CommandResult(0, json.dumps({"ref": "refs/tags/" + receipt["tag"], "object": {"sha": "b" * 40, "type": "tag"}}), "")
        if args[0] == "api" and "/git/tags/" in args[1]:
            return CommandResult(0, json.dumps({"sha": "b" * 40, "object": {"sha": "0" * 40, "type": "commit"}}), "")
        return remote.runner(args)

    restored = Store(tmp_path / "annotated", min_free_bytes=0)
    value(restored.init())
    assert value(restore_from_github(restored, "civic-source/cap-citation-pipeline", receipt["tag"], runner=runner, downloader=remote.downloader)) == snapshot_id


def test_later_inventory_context_gets_a_new_immutable_bundle_tag(tmp_path):
    from test_bulk_archive import add_inventory
    store, snapshot_id, _, _ = fixture(tmp_path)
    remote = Remote()
    options = {"runner": remote.runner, "downloader": remote.downloader}
    first = value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options))
    add_inventory(store)
    second = value(archive_to_github(store, snapshot_id, "civic-source/cap-citation-pipeline", **options))
    assert first["tag"] != second["tag"]
    assert len(remote.releases) == 2
