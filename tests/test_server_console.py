import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from mc_manager.agent import create_agent_app
from mc_manager.config import AgentConfig, AgentServer, ConsoleConfig
from mc_manager.server_console import (
    ConsoleCommandInvalid,
    ConsoleUnavailable,
    ServerConsole,
)


def console_config(tmp_path: Path, *, max_output_bytes: int = 256 * 1024) -> ConsoleConfig:
    return ConsoleConfig(
        input_pipe=tmp_path / ".manager" / "console.in",
        log_file=tmp_path / "logs" / "latest.log",
        max_command_bytes=64,
        max_output_bytes=max_output_bytes,
    )


def test_console_reads_incrementally_and_resets_after_log_rotation(tmp_path: Path):
    config = console_config(tmp_path, max_output_bytes=12)
    config.log_file.parent.mkdir()
    config.log_file.write_bytes(b"first\nsecond\n")
    console = ServerConsole(config)

    initial = console.read_output()
    assert initial["reset"] is True
    assert initial["content"].endswith("second\n")

    with config.log_file.open("ab") as handle:
        handle.write(b"third\n")
    incremental = console.read_output(initial["cursor"])
    assert incremental == {
        "content": "third\n",
        "cursor": config.log_file.stat().st_size,
        "reset": False,
    }

    config.log_file.write_bytes(b"new\n")
    rotated = console.read_output(incremental["cursor"])
    assert rotated == {"content": "new\n", "cursor": 4, "reset": True}


def test_console_validates_and_writes_one_minecraft_command(tmp_path: Path, monkeypatch):
    config = console_config(tmp_path)
    console = ServerConsole(config)
    written: list[bytes] = []

    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: 17)
    monkeypatch.setattr(os, "fstat", lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr(os, "write", lambda _descriptor, payload: written.append(payload) or len(payload))
    monkeypatch.setattr(os, "close", lambda _descriptor: None)

    assert console.send_command("/say hello players") == {
        "accepted": True,
        "command": "say",
    }
    assert written == [b"say hello players\n"]

    with pytest.raises(ConsoleCommandInvalid, match="one printable line"):
        console.send_command("say hello\nstop")


def test_console_reports_a_stopped_server_without_blocking(tmp_path: Path, monkeypatch):
    console = ServerConsole(console_config(tmp_path))
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))

    with pytest.raises(ConsoleUnavailable, match="start the server"):
        console.send_command("list")


async def test_agent_console_api_is_authenticated_and_explicitly_opt_in(tmp_path: Path):
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
        console=console_config(tmp_path),
    )
    app = create_agent_app(
        AgentConfig("test-agent", "127.0.0.1", 8766, "test-token", (server,))
    )
    fake_console = Mock()
    fake_console.read_output.return_value = {
        "content": "[Server thread/INFO]: Done\n",
        "cursor": 27,
        "reset": True,
    }
    fake_console.send_command.return_value = {"accepted": True, "command": "list"}
    app.state.runtime.consoles["survival"] = fake_console
    headers = {"Authorization": "Bearer test-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/servers/survival/console")).status_code == 401
        status_response = await client.get("/v1/servers", headers=headers)
        assert status_response.json()[0]["console_enabled"] is True
        output = await client.get(
            "/v1/servers/survival/console",
            params={"cursor": 10},
            headers=headers,
        )
        command = await client.post(
            "/v1/servers/survival/console",
            json={"command": "list"},
            headers=headers,
        )

    assert output.status_code == 200
    assert output.json()["cursor"] == 27
    assert command.status_code == 200
    fake_console.read_output.assert_called_once_with(10)
    fake_console.send_command.assert_called_once_with("list")
