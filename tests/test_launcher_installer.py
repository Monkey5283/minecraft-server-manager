import hashlib
import json
from pathlib import Path
from threading import Event

import httpx
import pytest

from mc_manager.launcher.client import plan_installation
from mc_manager.launcher.installer import (
    InstallCancelled, InstallConflict, Operation, Transaction, install_release,
    instance_lock, recover_installation,
)
from mc_manager.launcher.instance import InstallReceipt, atomic_json, fingerprint, read_receipt
from mc_manager.launcher.models import ReleaseManifest
from mc_manager.launcher.settings import DesktopSettings, load_settings, profile_directory, save_settings


def manifest(version="1", files=None, profile="test-pack"):
    return ReleaseManifest(profile_id=profile, version=version, minecraft_version="1.21.1", java_major=21,
                           loader={"kind": "fabric", "version": "0.16.0"}, files=files or [])


def entry(path="mods/a.jar", data=b"content"):
    return {"path": path, "url": "https://example.test/" + path,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def transport(data=b"content", calls=None):
    def respond(request):
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=data)
    return httpx.MockTransport(respond)


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def test_install_verify_update_remove_and_preserve_unmanaged(tmp_path):
    calls = []
    old = manifest(files=[entry(), entry("mods/old.jar")])
    write(tmp_path, "saves/world/data", b"world")
    write(tmp_path, "mods/user.jar", b"user-owned")
    result = install_release(old, tmp_path, transport=transport(calls=calls))
    assert result["installed"] and not result["launch_ready"]
    assert len(calls) == 2
    assert read_receipt(tmp_path).manifest == old
    assert (tmp_path / "mods/a.jar").read_bytes() == b"content"
    assert not (tmp_path / ".launcher/transaction.json").exists()
    install_release(old, tmp_path, transport=transport(calls=calls))
    assert len(calls) == 2  # A verified file needs no download on repair.
    new = manifest("2", files=[entry(data=b"updated")])
    plan = plan_installation(new, tmp_path)
    assert plan["conflicts"] == []
    assert plan["removals"][0]["path"] == "mods/old.jar"
    result = install_release(new, tmp_path, transport=transport(b"updated"), expected_plan_id=plan["plan_id"])
    assert (tmp_path / "mods/a.jar").read_bytes() == b"updated"
    assert not (tmp_path / "mods/old.jar").exists()
    assert (tmp_path / "mods/user.jar").read_bytes() == b"user-owned"
    assert (tmp_path / "saves/world/data").read_bytes() == b"world"
    assert (Path(result["backup_directory"]) / "0").read_bytes() == b"content"
    assert (Path(result["backup_directory"]) / "1").read_bytes() == b"content"
    assert read_receipt(tmp_path).manifest.version == "2"


def test_local_changes_require_review_and_remain_in_backup(tmp_path):
    old = manifest(files=[entry("config/pack.toml")])
    install_release(old, tmp_path, transport=transport())
    write(tmp_path, "config/pack.toml", b"player changes")
    new = manifest("2", files=[entry("config/pack.toml", b"new")])
    plan = plan_installation(new, tmp_path)
    assert plan["conflicts"] == ["config/pack.toml"]
    with pytest.raises(InstallConflict, match="approval"):
        install_release(new, tmp_path, transport=transport(b"new"))
    assert (tmp_path / "config/pack.toml").read_bytes() == b"player changes"
    result = install_release(new, tmp_path, transport=transport(b"new"), replace_modified=True, expected_plan_id=plan["plan_id"])
    assert (Path(result["backup_directory"]) / "0").read_bytes() == b"player changes"
    assert (tmp_path / "config/pack.toml").read_bytes() == b"new"


def test_unowned_collision_and_modified_obsolete_file_are_conflicts(tmp_path):
    write(tmp_path, "mods/a.jar", b"user")
    with pytest.raises(InstallConflict):
        install_release(manifest(files=[entry()]), tmp_path, transport=transport())
    install_release(manifest(files=[entry()]), tmp_path, transport=transport(), replace_modified=True)
    write(tmp_path, "mods/a.jar", b"user changed again")
    plan = plan_installation(manifest("2"), tmp_path)
    assert plan["conflicts"] == ["mods/a.jar"]
    with pytest.raises(InstallConflict):
        install_release(manifest("2"), tmp_path)
    assert (tmp_path / "mods/a.jar").read_bytes() == b"user changed again"


@pytest.mark.parametrize("body", [b"too long", b"short", b"wrong!!"])
def test_bad_download_never_changes_live_files_or_receipt(tmp_path, body):
    old = manifest(files=[entry()])
    install_release(old, tmp_path, transport=transport())
    before = (tmp_path / ".launcher/installed.json").read_bytes()
    with pytest.raises(ValueError, match="size|SHA-256"):
        install_release(manifest("2", files=[entry(data=b"updated")]), tmp_path, transport=transport(body))
    assert (tmp_path / "mods/a.jar").read_bytes() == b"content"
    assert (tmp_path / ".launcher/installed.json").read_bytes() == before
    assert not (tmp_path / ".launcher/transaction.json").exists()


def test_download_failure_and_cancellation_keep_pack_intact(tmp_path):
    old = manifest(files=[entry()])
    install_release(old, tmp_path, transport=transport())
    cancellation = Event()
    def cancel_on_download(event):
        if event["phase"] == "download":
            cancellation.set()
    with pytest.raises(InstallCancelled):
        install_release(manifest("2", files=[entry(data=b"updated")]), tmp_path, transport=transport(b"updated"),
                        cancel=cancellation, progress=cancel_on_download)
    assert read_receipt(tmp_path).manifest.version == "1"
    assert (tmp_path / "mods/a.jar").read_bytes() == b"content"
    def fail(request):
        raise httpx.ReadTimeout("test failure")
    with pytest.raises(httpx.ReadTimeout):
        install_release(manifest("2", files=[entry(data=b"updated")]), tmp_path, transport=httpx.MockTransport(fail))
    assert read_receipt(tmp_path).manifest.version == "1"


def test_cancel_mid_apply_rolls_back_replaced_removed_and_added_files(tmp_path):
    old = manifest(files=[entry(), entry("mods/obsolete.jar")])
    install_release(old, tmp_path, transport=transport())
    cancellation = Event()
    def cancel_on_apply(event):
        if event["phase"] == "apply" and event["completed"] == 3:
            cancellation.set()
    new = manifest("2", files=[entry(data=b"updated"), entry("mods/new.jar", b"updated")])
    with pytest.raises(InstallCancelled):
        install_release(new, tmp_path, transport=transport(b"updated"), cancel=cancellation, progress=cancel_on_apply)
    assert (tmp_path / "mods/a.jar").read_bytes() == b"content"
    assert (tmp_path / "mods/obsolete.jar").read_bytes() == b"content"
    assert not (tmp_path / "mods/new.jar").exists()
    assert read_receipt(tmp_path).manifest == old
    assert not (tmp_path / ".launcher/transaction.json").exists()


def interrupted(root, before=b"old", after=b"new"):
    tx = Transaction(id="a" * 32, operations=[Operation(path="config/a.txt", before={"size": len(before), "sha256": hashlib.sha256(before).hexdigest()},
                                                       after={"size": len(after), "sha256": hashlib.sha256(after).hexdigest()})])
    write(root, ".launcher/history/" + tx.id + "/backup/0", before)
    write(root, "config/a.txt", after)
    atomic_json(root / ".launcher/transaction.json", tx.model_dump())
    return tx


def test_recovery_after_interruption_is_repeatable(tmp_path):
    interrupted(tmp_path)
    with pytest.raises(ValueError, match="recovery"):
        plan_installation(manifest(), tmp_path)
    assert recover_installation(tmp_path)["state"] == "rolled_back"
    assert (tmp_path / "config/a.txt").read_bytes() == b"old"
    assert recover_installation(tmp_path) == {"recovered": False}


def test_recovery_never_overwrites_newer_edits_or_uses_bad_backup(tmp_path):
    tx = interrupted(tmp_path)
    write(tmp_path, "config/a.txt", b"newer user edit")
    with pytest.raises(InstallConflict, match="newer local change"):
        recover_installation(tmp_path)
    assert (tmp_path / "config/a.txt").read_bytes() == b"newer user edit"
    write(tmp_path, "config/a.txt", b"new")
    write(tmp_path, f".launcher/history/{tx.id}/backup/0", b"bad")
    with pytest.raises(InstallConflict, match="damaged"):
        recover_installation(tmp_path)
    assert (tmp_path / "config/a.txt").read_bytes() == b"new"
    assert (tmp_path / ".launcher/transaction.json").exists()


def test_receipt_is_commit_point_during_recovery(tmp_path):
    tx = interrupted(tmp_path)
    atomic_json(tmp_path / ".launcher/installed.json", InstallReceipt(transaction_id=tx.id, installed_at="test", manifest=manifest()).model_dump())
    write(tmp_path, "config/a.txt", b"player edit after commit")
    assert recover_installation(tmp_path)["state"] == "committed"
    assert (tmp_path / "config/a.txt").read_bytes() == b"player edit after commit"


def test_disk_full_and_stale_previews_do_not_change_files(tmp_path, monkeypatch):
    from collections import namedtuple
    target = manifest(files=[entry()])
    plan = plan_installation(target, tmp_path)
    write(tmp_path, "mods/a.jar", b"content")
    with pytest.raises(InstallConflict, match="since the preview"):
        install_release(target, tmp_path, expected_plan_id=plan["plan_id"])
    monkeypatch.setattr("mc_manager.launcher.installer.shutil.disk_usage", lambda path: namedtuple("Usage", "free")(0))
    with pytest.raises(ValueError, match="free space"):
        install_release(target, tmp_path)
    assert (tmp_path / "mods/a.jar").read_bytes() == b"content"


def test_instance_lock_releases_on_failure(tmp_path):
    with instance_lock(tmp_path):
        with pytest.raises(InstallConflict, match="Another launcher"):
            install_release(manifest(), tmp_path)
    assert install_release(manifest(), tmp_path)["installed"]


@pytest.mark.parametrize("redirect", ["http://other.test/a", "https://u:p@other.test/a", "file:///C:/test"])
def test_redirects_cannot_weaken_download_requirements(tmp_path, redirect):
    remote = httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": redirect}))
    with pytest.raises(ValueError):
        install_release(manifest(files=[entry()]), tmp_path, transport=remote)
    assert not (tmp_path / "mods/a.jar").exists()


def test_https_redirect_and_bounded_loop(tmp_path):
    def redirect(request):
        if request.url.host == "example.test":
            return httpx.Response(302, headers={"Location": "https://cdn.example.test/a.jar"})
        return httpx.Response(200, content=b"content")
    assert install_release(manifest(files=[entry()]), tmp_path, transport=httpx.MockTransport(redirect))["installed"]
    calls = []
    def loop(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": "https://example.test/loop"})
    with pytest.raises(ValueError, match="redirect"):
        install_release(manifest("2", files=[entry(data=b"new")]), tmp_path, transport=httpx.MockTransport(loop))
    assert len(calls) == 6


def test_reject_profile_switch_version_reuse_and_case_rename(tmp_path):
    old = manifest(files=[entry()])
    install_release(old, tmp_path, transport=transport())
    for new in [manifest(profile="different"), manifest(files=[entry(data=b"changed")]),
                manifest("2", files=[entry("mods/A.jar")])]:
        with pytest.raises(ValueError):
            plan_installation(new, tmp_path)


def test_target_changed_during_download_is_preserved(tmp_path):
    install_release(manifest(files=[entry()]), tmp_path, transport=transport())
    def respond(request):
        write(tmp_path, "mods/a.jar", b"user edit")
        return httpx.Response(200, content=b"updated")
    with pytest.raises(InstallConflict, match="during download"):
        install_release(manifest("2", files=[entry(data=b"updated")]), tmp_path, transport=httpx.MockTransport(respond))
    assert (tmp_path / "mods/a.jar").read_bytes() == b"user edit"
    assert read_receipt(tmp_path).manifest.version == "1"


def test_settings_roundtrip_and_safe_profile_directory(tmp_path):
    settings = load_settings(tmp_path)
    assert not (tmp_path / "settings.json").exists()
    settings.catalog_source = "https://example.test/catalog.json"
    save_settings(tmp_path, settings)
    assert load_settings(tmp_path) == settings
    assert profile_directory(settings, "con").name == "profile-con"
    with pytest.raises(ValueError):
        profile_directory(settings, "../../escape")
    with pytest.raises(ValueError):
        DesktopSettings(library_directory="relative")
    with pytest.raises(ValueError, match="credentials"):
        DesktopSettings(library_directory=str(tmp_path), catalog_source="https://user:password@example.test/catalog")


def test_cli_installs_empty_pack_and_recovers_without_catalog(tmp_path, capsys):
    from mc_manager.launcher.cli import main
    from mc_manager.launcher.models import Catalog, CatalogProfile
    path = tmp_path / "catalog.json"
    catalog = Catalog(profiles=[CatalogProfile(id="test-pack", name="Test", release=manifest())])
    path.write_text(catalog.model_dump_json())
    instance = tmp_path / "client"
    assert main(["--catalog", str(path), "install", "--profile", "test-pack", "--instance", str(instance)]) == 0
    assert json.loads(capsys.readouterr().out)["installed"]
    assert main(["recover", "--instance", str(instance)]) == 0
    assert json.loads(capsys.readouterr().out)["recovered"] is False
