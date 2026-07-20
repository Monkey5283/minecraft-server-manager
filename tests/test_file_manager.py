import sys
from pathlib import Path

import httpx
import pytest

from mc_manager.agent import create_agent_app
from mc_manager.config import AgentConfig, AgentServer, FileManagerConfig
from mc_manager.file_manager import (
    FileConflict,
    FileNotEditable,
    FileTooLarge,
    InvalidFilePath,
    ServerFileManager,
)


def test_file_manager_lists_reads_and_atomically_updates_text(tmp_path: Path):
    root = tmp_path / "server"
    root.mkdir()
    (root / "plugins").mkdir()
    config_file = root / "server.properties"
    config_file.write_bytes(b"motd=Original\n")
    manager = ServerFileManager(FileManagerConfig(root=root))

    listing = manager.list_directory("")
    assert [entry["name"] for entry in listing["entries"]] == [
        "plugins",
        "server.properties",
    ]

    opened = manager.read_text("server.properties")
    assert opened["content"] == "motd=Original\n"

    saved = manager.write_text(
        "server.properties", "motd=Monkeycraft\n", opened["version"]
    )
    assert saved["version"] != opened["version"]
    assert config_file.read_text(encoding="utf-8") == "motd=Monkeycraft\n"


def test_file_manager_rejects_traversal_symlinks_binary_and_oversized_text(
    tmp_path: Path,
):
    root = tmp_path / "server"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"abc\x00def")
    (root / "large.txt").write_text("x" * 20, encoding="utf-8")
    manager = ServerFileManager(
        FileManagerConfig(root=root, max_edit_size_bytes=10, max_upload_size_bytes=20)
    )

    with pytest.raises(InvalidFilePath):
        manager.read_text("../secret.txt")
    with pytest.raises(FileNotEditable):
        manager.read_text("binary.dat")
    download = manager.open_download("binary.dat")
    assert b"".join(download.iter_chunks(chunk_size=2)) == b"abc\x00def"
    assert download.handle.closed is True
    with pytest.raises(FileTooLarge):
        manager.read_text("large.txt")
    assert b"".join(manager.open_download("large.txt").iter_chunks()) == b"x" * 20

    with pytest.raises(InvalidFilePath):
        manager.open_download("../secret.txt")

    link = root / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    with pytest.raises(InvalidFilePath):
        manager.read_text("outside-link")
    with pytest.raises(InvalidFilePath):
        manager.open_download("outside-link")
    assert "outside-link" not in {
        entry["name"] for entry in manager.list_directory("")["entries"]
    }


def test_file_manager_detects_conflicts_and_controls_upload_overwrite(tmp_path: Path):
    root = tmp_path / "server"
    root.mkdir()
    target = root / "config.yml"
    target.write_bytes(b"value: one\n")
    manager = ServerFileManager(FileManagerConfig(root=root))
    opened = manager.read_text("config.yml")

    target.write_bytes(b"value: two\n")
    with pytest.raises(FileConflict, match="changed on disk"):
        manager.write_text("config.yml", "value: three\n", opened["version"])

    with pytest.raises(FileConflict, match="confirm overwrite"):
        manager.upload("config.yml", b"uploaded", overwrite=False)
    manager.upload("config.yml", b"uploaded", overwrite=True)
    assert target.read_bytes() == b"uploaded"

    manager.create_directory("mods")
    manager.write_text("mods/readme.txt", "new file\n", None)
    assert (root / "mods" / "readme.txt").read_text(encoding="utf-8") == "new file\n"


def test_file_manager_deletes_only_files_and_empty_directories(tmp_path: Path):
    root = tmp_path / "server"
    root.mkdir()
    manager = ServerFileManager(FileManagerConfig(root=root))
    (root / "delete.txt").write_text("remove me", encoding="utf-8")
    (root / "empty").mkdir()
    (root / "world").mkdir()
    (root / "world" / "level.dat").write_bytes(b"world")

    assert manager.delete("delete.txt") == {
        "path": "delete.txt",
        "kind": "file",
        "deleted": True,
    }
    assert manager.delete("empty")["kind"] == "directory"
    with pytest.raises(FileConflict, match="not empty"):
        manager.delete("world")
    with pytest.raises(InvalidFilePath):
        manager.delete("../outside")
    with pytest.raises(InvalidFilePath, match="unsupported characters"):
        manager.delete("bad\nname")


async def test_agent_file_api_is_authenticated_scoped_and_conflict_safe(tmp_path: Path):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    root = tmp_path / "server"
    root.mkdir()
    (root / "server.properties").write_bytes(b"motd=One\n")
    (root / "binary.dat").write_bytes(b"abc\x00def")
    server = AgentServer(
        id="survival",
        name="Survival",
        working_directory=root,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
        file_manager=FileManagerConfig(root=root, max_upload_size_bytes=1024),
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/servers/survival/files")).status_code == 401
        assert (
            await client.get(
                "/v1/servers/survival/files/download",
                params={"path": "binary.dat"},
            )
        ).status_code == 401
        assert (
            await client.delete(
                "/v1/servers/survival/files",
                params={"path": "binary.dat"},
            )
        ).status_code == 401
        status_response = await client.get("/v1/servers", headers=headers)
        assert status_response.json()[0]["files_enabled"] is True

        listing = await client.get("/v1/servers/survival/files", headers=headers)
        assert listing.status_code == 200
        assert {entry["name"] for entry in listing.json()["entries"]} == {
            "binary.dat",
            "server.properties",
        }

        traversal = await client.get(
            "/v1/servers/survival/files/content",
            params={"path": "../outside"},
            headers=headers,
        )
        assert traversal.status_code == 400

        opened = await client.get(
            "/v1/servers/survival/files/content",
            params={"path": "server.properties"},
            headers=headers,
        )
        version = opened.json()["version"]
        downloaded = await client.get(
            "/v1/servers/survival/files/download",
            params={"path": "binary.dat"},
            headers=headers,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"abc\x00def"
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert "filename*=UTF-8''binary.dat" in downloaded.headers[
            "content-disposition"
        ]
        assert downloaded.headers["cache-control"] == "no-store"

        download_traversal = await client.get(
            "/v1/servers/survival/files/download",
            params={"path": "../outside"},
            headers=headers,
        )
        assert download_traversal.status_code == 400
        saved = await client.put(
            "/v1/servers/survival/files/content",
            headers=headers,
            json={
                "path": "server.properties",
                "content": "motd=Two\n",
                "expected_version": version,
            },
        )
        assert saved.status_code == 200

        conflict = await client.put(
            "/v1/servers/survival/files/content",
            headers=headers,
            json={
                "path": "server.properties",
                "content": "motd=Three\n",
                "expected_version": version,
            },
        )
        assert conflict.status_code == 409

        uploaded = await client.put(
            "/v1/servers/survival/files/upload",
            params={"path": "plugins/example.jar"},
            headers=headers,
            content=b"jar bytes",
        )
        assert uploaded.status_code == 404

        created_directory = await client.post(
            "/v1/servers/survival/files/directory",
            headers=headers,
            json={"path": "plugins"},
        )
        assert created_directory.status_code == 200
        uploaded = await client.put(
            "/v1/servers/survival/files/upload",
            params={"path": "plugins/example.jar"},
            headers=headers,
            content=b"jar bytes",
        )
        assert uploaded.status_code == 200

        deleted = await client.delete(
            "/v1/servers/survival/files",
            params={"path": "plugins/example.jar"},
            headers=headers,
        )
        assert deleted.status_code == 200

    assert not (root / "plugins" / "example.jar").exists()


async def test_agent_file_api_is_explicitly_opt_in(tmp_path: Path):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    server = AgentServer(
        id="survival",
        name="Survival",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
    )
    app = create_agent_app(
        AgentConfig("test-agent", "127.0.0.1", 8766, "test-token", (server,))
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/servers/survival/files",
            headers={"Authorization": "Bearer test-token"},
        )
    assert response.status_code == 409
