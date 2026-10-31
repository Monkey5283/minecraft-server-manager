import json
from pathlib import Path
from threading import Event

import pytest

from mc_manager.launcher import cli
from mc_manager.launcher.game import runtime_key
from mc_manager.launcher.installer import InstallCancelled, install_release
from mc_manager.launcher.instance import atomic_json
from mc_manager.launcher.library import inspect_installation, inspect_library
from mc_manager.launcher.models import Catalog, CatalogProfile, ReleaseManifest
from mc_manager.launcher.settings import DesktopSettings, profile_directory


def release(version="1", profile_id="test"):
    return ReleaseManifest(profile_id=profile_id, version=version, minecraft_version="1.21.1", java_major=21)


def prepare(root, manifest=None):
    manifest = manifest or release()
    install_release(manifest, root)
    metadata = root / ".launcher/minecraft/versions/1.21.1/1.21.1.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}")
    java = root / "java.exe"
    java.write_bytes(b"not executed by inspection")
    atomic_json(root / ".launcher/game.json", {"schema_version": 1, "runtime_key": runtime_key(manifest),
                                               "version_id": "1.21.1", "java": str(java)})
    return manifest


def test_status_does_not_create_instances_or_require_network(tmp_path, monkeypatch):
    installed_root = tmp_path / "installed"
    prepare(installed_root)
    monkeypatch.setattr("httpx.Client", lambda *a, **k: pytest.fail("Status used the network"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("Status launched a process"))
    root = tmp_path / "absent"
    assert inspect_installation(release(), root).state == "not_installed"
    assert not root.exists()
    root = installed_root
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = inspect_installation(release(), root)
    assert result.state == "installed" and result.installed_version == "1"
    assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_updates_and_rollbacks_are_both_release_changes(tmp_path):
    prepare(tmp_path, release("2"))
    for version in ["1", "3", "next-season"]:
        result = inspect_installation(release(version), tmp_path)
        assert result.state == "release_changed"
        assert result.installed_version == "2"
        assert version in result.message


def test_profile_identity_and_immutable_release_conflicts(tmp_path):
    prepare(tmp_path)
    assert inspect_installation(release(profile_id="other"), tmp_path).state == "blocked"
    changed = release().model_copy(update={"memory_mb": 8192})
    assert inspect_installation(changed, tmp_path).label == "Release conflict"
    assert inspect_installation(release(), tmp_path).state == "installed"


@pytest.mark.parametrize("missing", [".launcher/game.json", ".launcher/minecraft/versions/1.21.1/1.21.1.json", "java.exe"])
def test_missing_runtime_components_require_repair(tmp_path, missing):
    prepare(tmp_path)
    (tmp_path / missing).unlink()
    assert inspect_installation(release(), tmp_path).state == "repair"


def test_custom_java_is_respected_without_running_it(tmp_path):
    prepare(tmp_path)
    (tmp_path / "java.exe").unlink()
    custom = tmp_path / "custom-java.exe"
    custom.write_bytes(b"fixture")
    assert inspect_installation(release(), tmp_path, java=str(custom)).state == "installed"
    assert inspect_installation(release(), tmp_path, java="java").state == "repair"


def test_interrupted_install_takes_precedence_and_does_not_modify_files(tmp_path):
    prepare(tmp_path)
    transaction = tmp_path / ".launcher/transaction.json"
    transaction.write_text("{}")
    assert inspect_installation(release("2"), tmp_path).state == "recover"
    assert transaction.read_text() == "{}"


@pytest.mark.parametrize("value", [[], {"schema_version": 1, "runtime_key": "bad"},
                                    {"schema_version": 1, "runtime_key": runtime_key(release()), "version_id": "../../outside"}])
def test_invalid_runtime_records_are_not_ready(tmp_path, value):
    prepare(tmp_path)
    atomic_json(tmp_path / ".launcher/game.json", value)
    assert inspect_installation(release(), tmp_path).state == "repair"


def test_corrupt_pack_record_and_unsafe_paths_are_contained(tmp_path, monkeypatch):
    prepare(tmp_path)
    (tmp_path / ".launcher/installed.json").write_text("SENSITIVE invalid metadata")
    result = inspect_installation(release(), tmp_path)
    assert result.state == "blocked" and "SENSITIVE" not in result.message
    monkeypatch.setattr("mc_manager.launcher.instance.is_link", lambda p: p.name == ".launcher")
    assert inspect_installation(release(), tmp_path).state == "blocked"


def test_one_bad_instance_does_not_hide_the_rest_of_the_catalog(tmp_path):
    catalog = Catalog(profiles=[CatalogProfile(id=name, name=name, release=release(profile_id=name)) for name in ["one", "two"]])
    settings = DesktopSettings(library_directory=str(tmp_path))
    prepare(profile_directory(settings, "one"), catalog.profiles[0].release)
    two = profile_directory(settings, "two")
    two.write_text("not a directory")
    statuses = inspect_library(catalog, settings)
    assert statuses["one"].state == "installed"
    assert statuses["two"].state == "blocked"
    cancel = Event()
    cancel.set()
    with pytest.raises(InstallCancelled):
        inspect_library(catalog, settings, cancel=cancel)


def test_cli_status_and_saved_account_check(tmp_path, capsys, monkeypatch):
    catalog = Catalog(profiles=[CatalogProfile(id="test", name="Test", release=release())])
    source = tmp_path / "catalog.json"
    source.write_text(catalog.model_dump_json())
    assert cli.main(["--catalog", str(source), "status", "--profile", "test", "--instance", str(tmp_path / "absent")]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "not_installed"
    from types import SimpleNamespace
    observed = []
    monkeypatch.setattr(cli.MicrosoftAccounts, "refresh", lambda self, client_id, **kw: observed.append(client_id) or SimpleNamespace(name="Player"))
    client = "12345678-1234-1234-1234-123456789abc"
    assert cli.main(["--data-directory", str(tmp_path / "data"), "--client-id", client, "check-account"]) == 0
    assert observed == [client]
    assert "ownership verified" in capsys.readouterr().out
