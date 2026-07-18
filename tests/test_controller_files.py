from unittest.mock import AsyncMock

import httpx

from mc_manager.client import AgentUnavailable
from mc_manager.config import ControllerConfig, RemoteServer
from mc_manager.controller import create_controller_app


class DownloadStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"jar "
        yield b"bytes"


def controller_config() -> ControllerConfig:
    return ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session-secret",
        cookie_secure=False,
        discord_token="discord-token",
        discord_guild_id=None,
        announcement_channel_id=None,
        servers=(
            RemoteServer(
                id="survival",
                name="Survival",
                agent_url="http://agent:8766",
                token="agent-token",
            ),
        ),
        health_presence_enabled=False,
    )


async def test_controller_file_routes_require_dashboard_login_and_proxy_operations(
    monkeypatch,
):
    agents = AsyncMock()
    agents.files.return_value = {
        "path": "",
        "entries": [],
        "max_edit_size_bytes": 1024,
        "max_upload_size_bytes": 2048,
    }
    agents.file_content.return_value = {
        "path": "server.properties",
        "content": "motd=Monkeycraft\n",
        "version": "version-one",
    }
    agents.save_file.return_value = {
        "path": "server.properties",
        "size": 20,
        "version": "version-two",
    }
    agents.create_directory.return_value = {"path": "plugins", "created": True}
    agents.upload_file.return_value = {
        "path": "plugins/example.jar",
        "size": 9,
        "version": "upload-version",
    }
    agent_download = httpx.Response(
        200,
        headers={"Content-Length": "9"},
        stream=DownloadStream(),
        request=httpx.Request("GET", "http://agent/download"),
    )
    agents.download_file.return_value = agent_download
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(controller_config())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/api/servers/survival/files")
        assert unauthorized.status_code == 401
        unauthorized_download = await client.get(
            "/api/servers/survival/files/download",
            params={"path": "plugins/example.jar"},
        )
        assert unauthorized_download.status_code == 401
        agents.download_file.assert_not_awaited()

        logged_in = await client.post(
            "/api/login", json={"username": "admin", "password": "password"}
        )
        assert logged_in.status_code == 200

        listing = await client.get(
            "/api/servers/survival/files", params={"path": "plugins"}
        )
        assert listing.status_code == 200

        opened = await client.get(
            "/api/servers/survival/files/content",
            params={"path": "server.properties"},
        )
        assert opened.json()["content"] == "motd=Monkeycraft\n"

        downloaded = await client.get(
            "/api/servers/survival/files/download",
            params={"path": "plugins/example.jar"},
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"jar bytes"
        assert downloaded.headers["content-length"] == "9"
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert "filename*=UTF-8''example.jar" in downloaded.headers[
            "content-disposition"
        ]
        assert downloaded.headers["cache-control"] == "no-store"

        saved = await client.put(
            "/api/servers/survival/files/content",
            json={
                "path": "server.properties",
                "content": "motd=Updated\n",
                "expected_version": "version-one",
            },
        )
        assert saved.status_code == 200

        created = await client.post(
            "/api/servers/survival/files/directory", json={"path": "plugins"}
        )
        assert created.status_code == 200

        uploaded = await client.put(
            "/api/servers/survival/files/upload",
            params={"path": "plugins/example.jar", "overwrite": "true"},
            headers={"Content-Type": "application/octet-stream"},
            content=b"jar bytes",
        )
        assert uploaded.status_code == 200

    server = controller_config().servers[0]
    agents.files.assert_awaited_once_with(server, "plugins")
    agents.file_content.assert_awaited_once_with(server, "server.properties")
    agents.download_file.assert_awaited_once_with(server, "plugins/example.jar")
    agents.save_file.assert_awaited_once_with(
        server, "server.properties", "motd=Updated\n", "version-one"
    )
    agents.create_directory.assert_awaited_once_with(server, "plugins")
    agents.upload_file.assert_awaited_once_with(
        server, "plugins/example.jar", b"jar bytes", overwrite=True
    )
    assert agent_download.is_closed is True


async def test_controller_preserves_safe_agent_file_conflict_status(monkeypatch):
    agents = AsyncMock()
    agents.save_file.side_effect = AgentUnavailable(
        "Survival: file changed on disk", status_code=409
    )
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(controller_config())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/login", json={"username": "admin", "password": "password"}
        )
        response = await client.put(
            "/api/servers/survival/files/content",
            json={
                "path": "server.properties",
                "content": "motd=Updated\n",
                "expected_version": "old-version",
            },
        )

    assert response.status_code == 409
    assert "changed on disk" in response.json()["detail"]
