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
