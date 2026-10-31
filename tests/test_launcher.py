import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from mc_manager.config import ConfigError, ControllerConfig, RemoteServer, load_controller_config
from mc_manager.controller import create_controller_app
from mc_manager.launcher.cli import main
from mc_manager.launcher.client import MAX_CATALOG_BYTES, plan_installation, read_catalog
from mc_manager.launcher.models import Catalog, CatalogProfile, NewProfile, PackFile, ReleaseManifest, ReleaseSpec
from mc_manager.launcher.store import LauncherStore, LauncherStoreError


def release(version="1.0.0", **kwargs):
    return {"version": version, "minecraft_version": "1.21.1", "java_major": 21, **kwargs}


def pack_file(path="mods/example.jar", content=b"example"):
    return {"path": path, "url": "https://cdn.example.net/example.jar",
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


@pytest.fixture
def config(tmp_path):
    return ControllerConfig(
        bind="127.0.0.1", port=8080, web_username="admin", web_password="old-password",
        session_secret="test-secret", cookie_secure=False, discord_token="unused",
        discord_guild_id=None, announcement_channel_id=None, health_presence_enabled=False,
        discovery_enabled=False, agent_registry_file=tmp_path / "paired.json",
        servers=(RemoteServer("survival", "Survival", "http://private-agent", "agent-secret"),),
    )


@pytest.fixture
def make_app(config, monkeypatch):
    monkeypatch.setattr("mc_manager.controller.AgentClient", AsyncMock)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    return lambda enabled=False: create_controller_app(replace(config, launcher_public_catalog_enabled=enabled))


async def login(client, username="admin", password="old-password"):
    assert (await client.post("/api/login", json={"username": username, "password": password})).status_code == 200


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"})


async def test_admin_permissions_and_public_opt_in(make_app):
    app = make_app()
    async with client_for(app) as admin, client_for(app) as operator:
        assert (await admin.get("/api/launcher")).status_code == 401
        assert (await admin.get("/api/launcher/catalog")).status_code == 401
        assert (await admin.post("/api/launcher/profiles", json={"id": "x", "name": "X"})).status_code == 401
        assert (await admin.get("/launcher/v1/catalog")).status_code == 404
        await login(admin)
        await admin.post("/api/users", json={"username": "operator", "password": "operator-password"})
        await login(operator, "operator", "operator-password")
        requests = [("GET", "/api/launcher", None), ("GET", "/api/launcher/catalog", None),
                    ("POST", "/api/launcher/profiles", {"id": "x", "name": "X"}),
                    ("PUT", "/api/launcher/profiles/x", {"name": "X", "expected_revision": 1}),
                    ("GET", "/api/launcher/profiles/x/releases", None),
                    ("POST", "/api/launcher/profiles/x/releases", release()),
                    ("PUT", "/api/launcher/profiles/x/publication", {"version": "1.0.0", "expected_revision": 1})]
        for method, url, payload in requests:
            assert (await operator.request(method, url, json=payload)).status_code == 403
        assert (await admin.get("/api/launcher")).json()["public_catalog_enabled"] is False


async def test_publish_rollback_withdraw_persistence_and_no_private_metadata(make_app, tmp_path):
    app = make_app(True)
    async with client_for(app) as admin, client_for(app) as player:
        await login(admin)
        assert (await player.get("/launcher/v1/catalog")).json() == {"schema_version": 1, "profiles": []}
        profile = {"id": "vanilla", "name": "Our Vanilla", "server_id": "survival"}
        response = await admin.post("/api/launcher/profiles", json=profile)
        assert response.status_code == 201
        assert (await admin.post("/api/launcher/profiles", json=profile)).status_code == 409
        assert (await admin.post("/api/launcher/profiles", json={**profile, "id": "other", "server_id": "missing"})).status_code == 404
        base = "/api/launcher/profiles/vanilla"
        for version in ["1.0.0", "2.0.0"]:
            response = await admin.post(base + "/releases", json=release(version, server_address="play.example.net:25565"))
            assert response.status_code == 201
        assert (await admin.post(base + "/releases", json=release())).status_code == 409
        public_release = "/launcher/v1/profiles/vanilla/releases/1.0.0"
        assert (await player.get(public_release)).status_code == 404
        assert (await player.get("/launcher/v1/catalog")).json()["profiles"] == []
        response = await admin.put(base + "/publication", json={"version": "1.0.0", "expected_revision": 1})
        assert response.status_code == 200 and response.json()["revision"] == 2
        catalog_response = await player.get("/launcher/v1/catalog")
        assert catalog_response.headers["cache-control"] == "no-store"
        catalog = Catalog.model_validate(catalog_response.json())
        assert catalog.profiles[0].release.version == "1.0.0"
        assert (await player.get(public_release)).json() == catalog.profiles[0].release.model_dump()
        assert (await player.get("/launcher/v1/profiles/vanilla/releases/2.0.0")).status_code == 404
        exported = await admin.get("/api/launcher/catalog")
        assert exported.json() == catalog.model_dump()
        assert "attachment" in exported.headers["content-disposition"]
        for private in ("agent-secret", "private-agent", "old-password", "created_by", "server_id", "survival"):
            assert private not in exported.text
        # A stale administrator cannot overwrite a newer publication or metadata.
        assert (await admin.put(base + "/publication", json={"version": "2.0.0", "expected_revision": 1})).status_code == 409
        assert (await admin.put(base, json={"name": "Changed", "expected_revision": 1})).status_code == 409
        assert (await admin.put(base + "/publication", json={"version": "2.0.0", "expected_revision": 2})).status_code == 200
        assert (await admin.put(base + "/publication", json={"version": "1.0.0", "expected_revision": 3})).status_code == 200
        assert (await admin.put(base + "/publication", json={"version": "missing", "expected_revision": 4})).status_code == 404
        assert (await admin.put(base, json={"name": "Renamed", "description": "Hello", "expected_revision": 4})).status_code == 200
        assert (await player.get("/launcher/v1/catalog")).json()["profiles"][0]["name"] == "Renamed"
        # A separate process/store sees committed releases and current pointer.
        reloaded = LauncherStore(tmp_path / "launcher.sqlite3")
        assert reloaded.catalog().profiles[0].release.version == "1.0.0"
        assert len(reloaded.releases("vanilla")) == 2
        assert all(item["published_at"] for item in reloaded.releases("vanilla"))
        assert (await admin.put(base + "/publication", json={"version": None, "expected_revision": 5})).status_code == 200
        assert (await player.get("/launcher/v1/catalog")).json()["profiles"] == []
        assert (await player.get(public_release)).status_code == 404
        assert len(reloaded.releases("vanilla")) == 2
        # Anonymous access remains incapable of changing publication or reading drafts.
        assert (await player.put(base + "/publication", json={"version": "1.0.0", "expected_revision": 6})).status_code == 401
        assert (await player.get(base + "/releases")).status_code == 401


async def test_validation_and_cross_profile_releases(make_app):
    async with client_for(make_app(True)) as admin:
        await login(admin)
        for profile_id in ["vanilla", "modded"]:
            assert (await admin.post("/api/launcher/profiles", json={"id": profile_id, "name": profile_id})).status_code == 201
        assert (await admin.post("/api/launcher/profiles/vanilla/releases", json=release(files=[pack_file()]))).status_code == 422
        response = await admin.post("/api/launcher/profiles/modded/releases", json=release(
            loader={"kind": "neoforge", "version": "21.1.200"}, files=[pack_file()]))
        assert response.status_code == 201
        assert (await admin.put("/api/launcher/profiles/vanilla/publication", json={"version": "1.0.0", "expected_revision": 1})).status_code == 404
        assert (await admin.post("/api/launcher/profiles/missing/releases", json=release())).status_code == 404
        assert (await admin.get("/api/launcher/profiles/missing/releases")).status_code == 404


@pytest.mark.parametrize("path", ["../x", "/mods/x.jar", "C:/mods/x.jar", "mods/../x", "mods//x",
    "mods/./x", "mods/a\\b", "mods/NUL.jar", "mods/CONIN$", "config/COM¹.txt", "mods/trail.",
    "config/trail ", "mods/x:y", "mods/x\nx", "saves/world.dat", "options.txt", "libraries/x.jar"])
def test_rejects_unsafe_or_unmanaged_paths(path):
    with pytest.raises(ValidationError):
        PackFile(**pack_file(path))


@pytest.mark.parametrize("url", ["http://example.com/a", "file:///tmp/a", "https://u:p@example.com/a",
    "https://example.com/a#fragment", "https://example.com:99999/a", "https://example.com/a b", "https://example.com/a\\b"])
def test_rejects_unsafe_download_urls(url):
    with pytest.raises(ValidationError):
        PackFile(**{**pack_file(), "url": url})


@pytest.mark.parametrize("files", [[pack_file(), pack_file("mods/EXAMPLE.jar")],
    [pack_file("config/a"), pack_file("config/a/b")]])
def test_rejects_colliding_files(files):
    with pytest.raises(ValidationError):
        ReleaseSpec(**release(loader={"kind": "fabric", "version": "0.16.0"}, files=files))


@pytest.mark.parametrize("payload", [release(loader={"kind": "forge"}),
    release(loader={"kind": "vanilla", "version": "1"}), release(java_major=0),
    release(memory_mb=1), release(server_address="https://server/"),
    release(server_address="server:99999"), release(server_address="-bad.example"),
    release(server_address="server:"), release(server_address="[bad]"), release(java_major=True),
    release(files=[{**pack_file("config/a"), "size": -1}]),
    release(files=[{**pack_file("config/a"), "sha256": "bad"}]), release(command="run-me")])
def test_rejects_invalid_release_contract(payload):
    with pytest.raises(ValidationError):
        ReleaseSpec(**payload)


@pytest.mark.parametrize("address", ["localhost", "play.example.net:25565", "127.0.0.1", "[::1]:25565", None])
def test_accepts_minecraft_addresses(address):
    assert ReleaseSpec(**release(server_address=address)).server_address == address


def test_publication_race_preserves_one_winner(tmp_path):
    path = tmp_path / "launcher.sqlite3"
    store = LauncherStore(path)
    store.create_profile(NewProfile(id="pack", name="Pack"))
    for version in ["1", "2"]:
        store.create_release("pack", ReleaseSpec(**release(version)), "admin")

    def publish(version):
        try:
            LauncherStore(path).publish("pack", version, 1)
            return 200
        except LauncherStoreError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(publish, ["1", "2"])) == [200, 409]
    assert store.profiles()[0]["revision"] == 2


def test_client_plan_verifies_hashes_and_preserves_local_data(tmp_path):
    instance = tmp_path / "instance"
    (instance / "mods").mkdir(parents=True)
    (instance / "mods" / "keep.jar").write_bytes(b"same")
    (instance / "mods" / "changed.jar").write_bytes(b"oops")
    (instance / "mods" / "user.jar").write_bytes(b"user-owned")
    (instance / "saves").mkdir()
    (instance / "saves" / "world.dat").write_bytes(b"world")
    manifest = ReleaseManifest(profile_id="pack", **release(loader={"kind": "fabric", "version": "0.16.0"},
        files=[pack_file("mods/keep.jar", b"same"), pack_file("mods/changed.jar", b"good"),
               pack_file("mods/missing.jar", b"missing")]))
    before = {str(path): path.read_bytes() for path in instance.rglob("*") if path.is_file()}
    plan = plan_installation(manifest, instance)
    assert [item["action"] for item in plan["files"]] == ["keep", "replace", "download"]
    assert plan["download_bytes"] == 11
    assert plan["launch_ready"] is False
    assert {str(path): path.read_bytes() for path in instance.rglob("*") if path.is_file()} == before
    empty = tmp_path / "new-instance"
    assert plan_installation(ReleaseManifest(profile_id="vanilla", **release()), empty)["files"] == []
    assert not empty.exists()


def test_client_rejects_links_and_non_directories(tmp_path, monkeypatch):
    manifest = ReleaseManifest(profile_id="pack", **release(files=[pack_file("config/a.txt")]))
    (tmp_path / "config").write_text("not a directory")
    with pytest.raises(ValueError, match="non-directory"):
        plan_installation(manifest, tmp_path)
    with pytest.raises(ValueError, match="must be a directory"):
        plan_installation(manifest, tmp_path / "config")
    monkeypatch.setattr("mc_manager.launcher.instance.is_link", lambda path: path.name == "config")
    with pytest.raises(ValueError, match="symbolic link"):
        plan_installation(manifest, tmp_path)


def test_client_rejects_symlink_escape_when_supported(tmp_path):
    instance = tmp_path / "instance"
    outside = tmp_path / "outside"
    instance.mkdir()
    outside.mkdir()
    try:
        (instance / "config").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this host")
    manifest = ReleaseManifest(profile_id="pack", **release(files=[pack_file("config/a.txt")]))
    with pytest.raises(ValueError, match="symbolic link"):
        plan_installation(manifest, instance)


def test_cli_export_roundtrip_and_errors(tmp_path, capsys):
    store = LauncherStore(tmp_path / "launcher.sqlite3")
    store.create_profile(NewProfile(id="vanilla", name="Vanilla"))
    store.create_release("vanilla", ReleaseSpec(**release()), "admin")
    store.publish("vanilla", "1.0.0", 1)
    path = tmp_path / "catalog.json"
    path.write_text(store.catalog().model_dump_json(), encoding="utf-8")
    assert main(["--catalog", str(path), "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "vanilla"
    assert main(["--catalog", str(path), "plan", "--profile", "vanilla", "--instance", str(tmp_path / "instance")]) == 0
    assert json.loads(capsys.readouterr().out)["launch_ready"] is False
    assert main(["--catalog", str(path), "plan", "--profile", "missing", "--instance", str(tmp_path)]) == 1
    assert "not in the published catalog" in capsys.readouterr().err
    assert main(["--catalog", "http://example.test/catalog", "list"]) == 1
    assert "Use HTTPS" in capsys.readouterr().err


def test_catalog_size_schema_and_identity_checks(tmp_path):
    path = tmp_path / "catalog.json"
    for raw in ['{"schema_version": 2, "profiles": []}', "[]", "invalid"]:
        path.write_text(raw)
        with pytest.raises(ValueError):
            read_catalog(str(path))
    path.write_bytes(b" " * (MAX_CATALOG_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        read_catalog(str(path))
    with pytest.raises(ValidationError, match="does not match"):
        CatalogProfile(id="wrong", name="Wrong", release=ReleaseManifest(profile_id="pack", **release()))
    profile = CatalogProfile(id="pack", name="Pack", release=ReleaseManifest(profile_id="pack", **release()))
    with pytest.raises(ValidationError, match="unique"):
        Catalog(profiles=[profile, profile])


def test_network_catalog_download_is_bounded_and_rejects_redirects(monkeypatch):
    original_client = httpx.Client
    def response(request):
        if request.url.path == "/oversize":
            return httpx.Response(200, content=b" " * (MAX_CATALOG_BYTES + 1))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://other.test/catalog"})
        return httpx.Response(200, json={"schema_version": 1, "profiles": []})
    monkeypatch.setattr("mc_manager.launcher.client.httpx.Client",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs))
    assert read_catalog("https://example.test/catalog").profiles == []
    assert read_catalog("http://localhost/catalog", allow_http=True).profiles == []
    with pytest.raises(ValueError, match="size limit"):
        read_catalog("https://example.test/oversize")
    with pytest.raises(httpx.HTTPStatusError):
        read_catalog("https://example.test/redirect")
    with pytest.raises(ValueError, match="credentials"):
        read_catalog("https://user:secret@example.test/catalog")


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ('"false"', None)])
def test_launcher_config_is_explicit_boolean(tmp_path, monkeypatch, value, expected):
    for name in ["MC_WEB_PASSWORD", "MC_SESSION_SECRET", "DISCORD_BOT_TOKEN"]:
        monkeypatch.setenv(name, "test-value")
    path = tmp_path / "controller.toml"
    path.write_text(f"[launcher]\npublic_catalog_enabled = {value}\n")
    if expected is None:
        with pytest.raises(ConfigError, match="must be a boolean"):
            load_controller_config(path)
    else:
        assert load_controller_config(path).launcher_public_catalog_enabled is expected
