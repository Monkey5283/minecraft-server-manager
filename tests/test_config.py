from pathlib import Path

import pytest

from mc_manager.config import ConfigError, load_agent_config, load_controller_config


def test_agent_loads_allowlisted_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    config = tmp_path / "agent.toml"
    config.write_text(
        """
[agent]
token_env = "TEST_AGENT_TOKEN"

[[servers]]
id = "survival"
working_directory = "."
[servers.actions]
start = ["service", "start"]
stop = ["service", "stop"]
restart = ["service", "restart"]
status = ["service", "status"]
[servers.scripts]
backup = [["backup-tool", "survival"]]
""",
        encoding="utf-8",
    )
    loaded = load_agent_config(config)
    assert loaded.servers[0].actions["start"] == (("service", "start"),)
    assert loaded.servers[0].scripts["backup"] == (("backup-tool", "survival"),)


def test_agent_rejects_arbitrary_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    config = tmp_path / "agent.toml"
    config.write_text(
        """
[agent]
token_env = "TEST_AGENT_TOKEN"
[[servers]]
id = "survival"
[servers.actions]
start = ["service", "start"]
stop = ["service", "stop"]
restart = ["service", "restart"]
status = ["service", "status"]
destroy = ["bad-command"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unsupported actions"):
        load_agent_config(config)


def test_controller_requires_valid_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("WEB", "SESSION", "DISCORD", "AGENT"):
        monkeypatch.setenv(name, "secret")
    config = tmp_path / "controller.toml"
    config.write_text(
        """
[auth]
web_password_env = "WEB"
session_secret_env = "SESSION"
[discord]
discord_token_env = "DISCORD"
[[servers]]
id = "survival"
agent_url = "192.168.1.20:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="agent_url"):
        load_controller_config(config)


def test_controller_loads_announcement_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name in ("WEB", "SESSION", "DISCORD", "AGENT"):
        monkeypatch.setenv(name, "secret")
    config = tmp_path / "controller.toml"
    config.write_text(
        """
[auth]
web_password_env = "WEB"
session_secret_env = "SESSION"
[discord]
discord_token_env = "DISCORD"
guild_id = 123
announcement_channel_id = 456
[[servers]]
id = "survival"
agent_url = "http://192.168.1.20:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )

    loaded = load_controller_config(config)

    assert loaded.discord_guild_id == 123
    assert loaded.announcement_channel_id == 456


def test_controller_loads_ups_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("WEB", "SESSION", "DISCORD", "AGENT"):
        monkeypatch.setenv(name, "secret")
    config = tmp_path / "controller.toml"
    config.write_text(
        """
[auth]
web_password_env = "WEB"
session_secret_env = "SESSION"
[discord]
discord_token_env = "DISCORD"
[ups]
enabled = true
ups_name = "cyberpower"
status_command = ["/usr/bin/upsc", "cyberpower", "ups.status"]
poll_interval_seconds = 10
on_battery_delay_seconds = 20
stop_timeout_seconds = 90
downstream_shutdown_script = "shutdown_host"
local_shutdown_delay_seconds = 5
local_shutdown_command = ["/usr/bin/systemctl", "poweroff"]
[[servers]]
id = "survival"
agent_url = "http://192.168.1.20:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )

    loaded = load_controller_config(config)

    assert loaded.ups.enabled is True
    assert loaded.ups.ups_name == "cyberpower"
    assert loaded.ups.status_command == ("/usr/bin/upsc", "cyberpower", "ups.status")
    assert loaded.ups.poll_interval_seconds == 10
    assert loaded.ups.downstream_shutdown_script == "shutdown_host"


def test_linuxgsm_example_uses_allowlisted_commands(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MC_AGENT_TOKEN", "secret")

    loaded = load_agent_config(
        Path(__file__).parents[1] / "config/agent.linuxgsm.example.toml"
    )
    server = loaded.servers[0]

    assert server.actions["start"][0][-2:] == (
        "/home/mcserver/mcserver",
        "start",
    )
    assert server.actions["update"][0][-1] == "update"
    assert server.actions["status"][0][-3:] == (
        "has-session",
        "-t",
        "=mcserver",
    )
    assert "backup" in server.scripts
    assert "update_linuxgsm" in server.scripts
