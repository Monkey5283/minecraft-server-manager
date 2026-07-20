from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .config import ConsoleConfig


class ServerConsoleError(RuntimeError):
    status_code = 400


class ConsoleUnavailable(ServerConsoleError):
    status_code = 409


class ConsoleAccessDenied(ServerConsoleError):
    status_code = 403


class ConsoleCommandInvalid(ServerConsoleError):
    status_code = 400


class ServerConsole:
    def __init__(self, config: ConsoleConfig):
        self.config = config

    @staticmethod
    def _safe_open_flags(base: int) -> int:
        return base | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    def send_command(self, raw_command: str) -> dict:
        command = raw_command.strip()
        if command.startswith("/"):
            command = command[1:].lstrip()
        if not command:
            raise ConsoleCommandInvalid("Enter a Minecraft server command")
        encoded = command.encode("utf-8")
        if len(encoded) > self.config.max_command_bytes:
            raise ConsoleCommandInvalid(
                f"Command exceeds the {self.config.max_command_bytes}-byte limit"
            )
        if any(character in command for character in "\r\n\x00") or any(
            ord(character) < 32 or ord(character) == 127 for character in command
        ):
            raise ConsoleCommandInvalid("Console commands must be one printable line")

        descriptor = -1
        try:
            descriptor = os.open(
                self.config.input_pipe,
                self._safe_open_flags(os.O_WRONLY | getattr(os, "O_NONBLOCK", 0)),
            )
            pipe_stat = os.fstat(descriptor)
            if not stat.S_ISFIFO(pipe_stat.st_mode):
                raise ConsoleUnavailable("Configured console input is not a named pipe")
            payload = encoded + b"\n"
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise ConsoleUnavailable("Minecraft console accepted only part of the command")
        except FileNotFoundError as exc:
            raise ConsoleUnavailable(
                "Minecraft console is unavailable; start the server first"
            ) from exc
        except PermissionError as exc:
            raise ConsoleAccessDenied("Minecraft console input is not writable") from exc
        except OSError as exc:
            if exc.errno in {errno.ENXIO, errno.ENODEV, errno.EPIPE}:
                raise ConsoleUnavailable(
                    "Minecraft console is unavailable; start the server first"
                ) from exc
            raise ConsoleUnavailable(f"Could not write to Minecraft console: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return {"accepted": True, "command": command.split(maxsplit=1)[0]}

    def read_output(self, cursor: int = 0) -> dict:
        if cursor < 0:
            raise ServerConsoleError("Console cursor must not be negative")
        descriptor = -1
        try:
            descriptor = os.open(
                self.config.log_file,
                self._safe_open_flags(os.O_RDONLY),
            )
            log_stat = os.fstat(descriptor)
            if not stat.S_ISREG(log_stat.st_mode):
                raise ConsoleUnavailable("Configured console output is not a regular file")
            reset = cursor == 0 or cursor > log_stat.st_size
            start = (
                max(0, log_stat.st_size - self.config.max_output_bytes)
                if reset
                else cursor
            )
            os.lseek(descriptor, start, os.SEEK_SET)
            content = os.read(descriptor, self.config.max_output_bytes)
            next_cursor = start + len(content)
        except FileNotFoundError:
            return {"content": "", "cursor": 0, "reset": cursor != 0}
        except PermissionError as exc:
            raise ConsoleAccessDenied("Minecraft console log is not readable") from exc
        except OSError as exc:
            raise ConsoleUnavailable(f"Could not read Minecraft console log: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return {
            "content": content.decode("utf-8", errors="replace"),
            "cursor": next_cursor,
            "reset": reset,
        }


__all__ = [
    "ConsoleAccessDenied",
    "ConsoleCommandInvalid",
    "ConsoleUnavailable",
    "ServerConsole",
    "ServerConsoleError",
]
