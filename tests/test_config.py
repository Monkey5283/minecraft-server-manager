import json
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


def test_agent_allows_empty_onboarding_config_and_loads_managed_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    managed = tmp_path / "managed.json"
    managed.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "paper",
                        "working_directory": str(tmp_path / "paper"),
                        "actions": {
                            "start": ["service", "start"],
                            "stop": ["service", "stop"],
                            "restart": ["service", "restart"],
                            "status": ["service", "status"],
                        },
                    }
                ]
            }
        )
    )
    config = tmp_path / "agent.toml"
    config.write_text(
        f'''[agent]
token_env = "TEST_AGENT_TOKEN"
[provisioning]
enabled = true
managed_servers_file = {str(managed)!r}
'''
    )

    loaded = load_agent_config(config)

    assert loaded.provisioning_enabled is True
    assert loaded.servers[0].id == "paper"
    assert loaded.servers[0].console is not None
    assert loaded.servers[0].console.log_file == (tmp_path / "paper/logs/latest.log").resolve()


def test_agent_loads_opt_in_file_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    server_root = tmp_path / "server"
    config = tmp_path / "agent.toml"
    config.write_text(
        f"""
[agent]
token_env = "TEST_AGENT_TOKEN"
[[servers]]
id = "survival"
working_directory = {str(server_root)!r}
[servers.file_manager]
enabled = true
root = "."
max_edit_size_bytes = 4096
max_upload_size_bytes = 8192
[servers.console]
enabled = true
input_pipe = ".manager/console.in"
log_file = "logs/latest.log"
[servers.actions]
start = ["service", "start"]
stop = ["service", "stop"]
restart = ["service", "restart"]
status = ["service", "status"]
""",
        encoding="utf-8",
    )

    loaded = load_agent_config(config)
    files = loaded.servers[0].file_manager

    assert files is not None
    assert files.root == server_root.resolve()
    assert files.max_edit_size_bytes == 4096
    assert files.max_upload_size_bytes == 8192
    assert loaded.servers[0].console is not None
    assert loaded.servers[0].console.input_pipe == (server_root / ".manager/console.in").resolve()


def test_agent_rejects_console_paths_outside_server_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    server_root = tmp_path / "server"
    config = tmp_path / "agent.toml"
    config.write_text(
        f'''
[agent]
token_env = "TEST_AGENT_TOKEN"
[[servers]]
id = "survival"
working_directory = {str(server_root)!r}
[servers.console]
enabled = true
input_pipe = "../outside/console.in"
log_file = "logs/latest.log"
[servers.actions]
start = ["service", "start"]
stop = ["service", "stop"]
restart = ["service", "restart"]
status = ["service", "status"]
''',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must stay inside working_directory"):
        load_agent_config(config)


def test_agent_rejects_unsafe_file_manager_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    config = tmp_path / "agent.toml"
    config.write_text(
        """
[agent]
token_env = "TEST_AGENT_TOKEN"
[[servers]]
id = "survival"
[servers.file_manager]
enabled = true
max_edit_size_bytes = 999999999
[servers.actions]
start = ["service", "start"]
stop = ["service", "stop"]
restart = ["service", "restart"]
status = ["service", "status"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_edit_size_bytes"):
        load_agent_config(config)


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
    assert loaded.ups.charge_command == (
        "/usr/bin/upsc",
        "cyberpower",
        "battery.charge",
    )
    assert loaded.ups.poll_interval_seconds == 10
    assert loaded.ups.downstream_shutdown_script == "shutdown_host"


def test_controller_derives_ups_charge_command_from_status_command(
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
[ups]
enabled = true
status_command = ["/usr/bin/upsc", "cyberpower@localhost", "ups.status"]
[[servers]]
id = "survival"
agent_url = "http://192.168.1.20:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )

    loaded = load_controller_config(config)

    assert loaded.ups.charge_command == (
        "/usr/bin/upsc",
        "cyberpower@localhost",
        "battery.charge",
    )


def test_controller_defaults_health_presence_and_ups_card_to_enabled(
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
announcement_channel_id = 456
[ups]
enabled = true
[[servers]]
id = "survival"
agent_url = "http://192.168.1.20:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )

    loaded = load_controller_config(config)

    assert loaded.health_presence_enabled is True
    assert loaded.health_poll_interval_seconds == 30
    assert loaded.ups.discord_status_enabled is True
    assert loaded.ups.discord_status_channel_id == 456


def test_controller_loads_health_toggles_and_custom_ups_channel(
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
announcement_channel_id = 456
health_presence_enabled = false
health_poll_interval_seconds = 12
[ups]
enabled = true
discord_status_enabled = false
discord_status_channel_id = 789
[[servers]]
id = "survival"
agent_url = "http://192.168.1.20:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )

    loaded = load_controller_config(config)

    assert loaded.health_presence_enabled is False
    assert loaded.health_poll_interval_seconds == 12
    assert loaded.ups.discord_status_enabled is False
    assert loaded.ups.discord_status_channel_id == 789


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


def test_agent_loads_player_query_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_AGENT_TOKEN", "secret")
    config = tmp_path / "agent.toml"
    config.write_text(
        """
[agent]
token_env = "TEST_AGENT_TOKEN"

[[servers]]
id = "lobby"
[servers.actions]
start = ["service", "start"]
stop = ["service", "stop"]
restart = ["service", "restart"]
status = ["service", "status"]
[servers.player_query]
host = "127.0.0.1"
port = 25566
timeout_seconds = 2.5
""",
        encoding="utf-8",
    )

    loaded = load_agent_config(config)

    assert loaded.servers[0].player_query is not None
    assert loaded.servers[0].player_query.host == "127.0.0.1"
    assert loaded.servers[0].player_query.port == 25566
    assert loaded.servers[0].player_query.timeout_seconds == 2.5
    assert loaded.servers[0].player_query.offline_status_codes == (3,)


def test_controller_loads_player_tracking(
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
announcement_channel_id = 456
[player_tracking]
enabled = true
poll_interval_seconds = 3
leave_grace_seconds = 12
[[servers]]
id = "lobby"
agent_url = "http://192.168.1.35:8766"
token_env = "AGENT"
track_players = true
""",
        encoding="utf-8",
    )

    loaded = load_controller_config(config)

    assert loaded.player_tracking.enabled is True
    assert loaded.player_tracking.channel_id == 456
    assert loaded.player_tracking.poll_interval_seconds == 3
    assert loaded.player_tracking.leave_grace_seconds == 12
    assert loaded.servers[0].track_players is True


def test_controller_rejects_tracking_without_a_tracked_server(
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
announcement_channel_id = 456
[player_tracking]
enabled = true
[[servers]]
id = "velocity"
agent_url = "http://192.168.1.35:8766"
token_env = "AGENT"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="track_players"):
        load_controller_config(config)
