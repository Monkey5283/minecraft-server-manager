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
    with pytest.raises(FileTooLarge):
        manager.read_text("large.txt")

    link = root / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    with pytest.raises(InvalidFilePath):
        manager.read_text("outside-link")
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


async def test_agent_file_api_is_authenticated_scoped_and_conflict_safe(tmp_path: Path):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    root = tmp_path / "server"
    root.mkdir()
    (root / "server.properties").write_bytes(b"motd=One\n")
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
        status_response = await client.get("/v1/servers", headers=headers)
        assert status_response.json()[0]["files_enabled"] is True

        listing = await client.get("/v1/servers/survival/files", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["entries"][0]["name"] == "server.properties"

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

    assert (root / "plugins" / "example.jar").read_bytes() == b"jar bytes"


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
