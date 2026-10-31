import io
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest

from mc_manager.folder_transfer import build_folder_archive
from mc_manager.file_manager import FileManagerError
from test_controller_files import controller_config
from mc_manager.controller import create_controller_app


def agent():
    agents = AsyncMock()
    agents.files.side_effect = [
        {"entries": [{"name": "empty", "kind": "directory"}, {"name": "data.bin", "kind": "file"}]},
        {"entries": []},
    ]
    agents.download_file.return_value = httpx.Response(200, content=b"\x00world data")
    return agents


async def test_archive_preserves_nested_empty_directories_and_binary_files():
    agents = agent()
    handle, name, size = await build_folder_archive(agents, controller_config().servers[0], "world")
    try:
        assert name == "world.zip"
        assert size > 0
        with zipfile.ZipFile(handle) as archive:
            assert set(archive.namelist()) == {"world/", "world/empty/", "world/data.bin"}
            assert archive.read("world/data.bin") == b"\x00world data"
        assert agents.download_file.return_value.is_closed
    finally:
        handle.close()


async def test_archive_uses_disk_directory_and_rejects_low_space(monkeypatch, tmp_path):
    import tempfile
    from unittest.mock import Mock
    from mc_manager.folder_transfer import ArchiveStorageError
    real_temporary_file = tempfile.TemporaryFile
    create = Mock(side_effect=real_temporary_file)
    monkeypatch.setattr('mc_manager.folder_transfer.tempfile.TemporaryFile', create)
    handle, _, _ = await build_folder_archive(agent(), controller_config().servers[0], 'world', directory=tmp_path)
    handle.close()
    create.assert_called_once_with(dir=tmp_path)
    monkeypatch.setattr('mc_manager.folder_transfer.shutil.disk_usage', lambda path: Mock(free=1))
    with pytest.raises(ArchiveStorageError):
        await build_folder_archive(agent(), controller_config().servers[0], 'world', directory=tmp_path)


@pytest.mark.parametrize("name", ["../secret", "..", "/etc", "a\\b"])
async def test_archive_rejects_unsafe_entries(name):
    agents = agent()
    agents.files.side_effect = [{"entries": [{"name": name, "kind": "file"}]}]
    with pytest.raises(FileManagerError):
        await build_folder_archive(agents, controller_config().servers[0], "world")
    agents.download_file.assert_not_awaited()


async def test_archive_enforces_size_limit_and_closes_response(monkeypatch):
    monkeypatch.setattr("mc_manager.folder_transfer.MAX_ARCHIVE_BYTES", 1)
    agents = agent()
    with pytest.raises(FileManagerError, match="20 GiB"):
        await build_folder_archive(agents, controller_config().servers[0], "world")
    assert agents.download_file.return_value.is_closed


async def test_archive_route_authentication_zip_and_lock_cleanup(monkeypatch):
    agents = agent()
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(controller_config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}) as client:
        url = "/api/servers/survival/files/archive?path=world"
        assert (await client.get(url)).status_code == 401
        agents.files.assert_not_awaited()
        await client.post("/api/login", json={"username": "admin", "password": "password"})
        response = await client.get(url)
        assert response.status_code == 200
        assert "world.zip" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.read("world/data.bin") == b"\x00world data"
        agents.files.side_effect = [{"entries": []}]
        assert (await client.get(url)).status_code == 200
        assert (await client.get(url + "/../..")).status_code == 400
