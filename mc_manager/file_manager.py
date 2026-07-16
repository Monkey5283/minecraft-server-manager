from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .config import FileManagerConfig


class FileManagerError(RuntimeError):
    status_code = 400


class InvalidFilePath(FileManagerError):
    status_code = 400


class ManagedFileNotFound(FileManagerError):
    status_code = 404


class FileAccessDenied(FileManagerError):
    status_code = 403


class FileConflict(FileManagerError):
    status_code = 409


class FileTooLarge(FileManagerError):
    status_code = 413


class FileNotEditable(FileManagerError):
    status_code = 415


def _version(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ServerFileManager:
    def __init__(self, config: FileManagerConfig):
        self.config = config
        self.root = config.root.resolve()

    @staticmethod
    def normalize_path(raw_path: str, *, allow_root: bool = True) -> str:
        if "\x00" in raw_path or "\\" in raw_path:
            raise InvalidFilePath("File path contains unsupported characters")
        if raw_path in {"", ".", "/"}:
            if allow_root:
                return ""
            raise InvalidFilePath("A file or directory name is required")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise InvalidFilePath("File path must stay inside the configured server root")
        return "/".join(path.parts)

    def _resolve(self, raw_path: str, *, allow_root: bool = True) -> tuple[str, Path]:
        normalized = self.normalize_path(raw_path, allow_root=allow_root)
        target = (self.root / normalized).resolve(strict=False)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise InvalidFilePath(
                "File path must stay inside the configured server root"
            ) from exc
        return normalized, target

    @staticmethod
    def _translate_os_error(exc: OSError, action: str) -> FileManagerError:
        if isinstance(exc, FileNotFoundError):
            return ManagedFileNotFound(f"{action}: file or directory not found")
        if isinstance(exc, PermissionError):
            return FileAccessDenied(f"{action}: permission denied")
        return FileManagerError(f"{action}: {exc}")

    def list_directory(self, raw_path: str) -> dict:
        normalized, directory = self._resolve(raw_path)
        if not directory.exists():
            raise ManagedFileNotFound("Directory not found")
        if not directory.is_dir():
            raise FileConflict("Requested path is not a directory")
        entries: list[dict] = []
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise self._translate_os_error(exc, "Could not list directory") from exc
        for child in children:
            child_path = f"{normalized}/{child.name}" if normalized else child.name
            try:
                _, resolved_child = self._resolve(child_path, allow_root=False)
                child_stat = resolved_child.stat()
            except (FileManagerError, OSError):
                # Do not expose broken links or links that resolve outside the root.
                continue
            is_directory = stat.S_ISDIR(child_stat.st_mode)
            is_file = stat.S_ISREG(child_stat.st_mode)
            if not (is_directory or is_file):
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": child_path,
                    "kind": "directory" if is_directory else "file",
                    "size": None if is_directory else child_stat.st_size,
                    "modified_ms": round(child_stat.st_mtime * 1000),
                    "editable": is_file
                    and child_stat.st_size <= self.config.max_edit_size_bytes,
                }
            )
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
        return {
            "path": normalized,
            "entries": entries,
            "max_edit_size_bytes": self.config.max_edit_size_bytes,
            "max_upload_size_bytes": self.config.max_upload_size_bytes,
        }

    def read_text(self, raw_path: str) -> dict:
        normalized, target = self._resolve(raw_path, allow_root=False)
        if not target.exists():
            raise ManagedFileNotFound("File not found")
        if not target.is_file():
            raise FileConflict("Requested path is not a file")
        try:
            size = target.stat().st_size
            if size > self.config.max_edit_size_bytes:
                raise FileTooLarge(
                    f"File is too large to edit ({size} bytes; limit is "
                    f"{self.config.max_edit_size_bytes})"
                )
            content = target.read_bytes()
        except FileManagerError:
            raise
        except OSError as exc:
            raise self._translate_os_error(exc, "Could not read file") from exc
        if b"\x00" in content:
            raise FileNotEditable("Binary files cannot be opened in the text editor")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileNotEditable("File is not valid UTF-8 text") from exc
        return {
            "path": normalized,
            "content": text,
            "size": len(content),
            "version": _version(content),
        }

    @staticmethod
    def _atomic_write(target: Path, content: bytes, existing_stat: os.stat_result | None) -> None:
        file_descriptor = -1
        temporary_path: Path | None = None
        try:
            file_descriptor, raw_temporary_path = tempfile.mkstemp(
                prefix=".mc-manager-", dir=target.parent
            )
            temporary_path = Path(raw_temporary_path)
            mode = stat.S_IMODE(existing_stat.st_mode) if existing_stat else 0o640
            if hasattr(os, "fchmod"):
                os.fchmod(file_descriptor, mode)
            else:
                os.chmod(temporary_path, mode)
            if existing_stat is not None and hasattr(os, "fchown"):
                try:
                    os.fchown(file_descriptor, existing_stat.st_uid, existing_stat.st_gid)
                except PermissionError:
                    # Directory default ACLs still preserve server-user access when
                    # the manager is not privileged to retain ownership.
                    pass
            with os.fdopen(file_descriptor, "wb") as handle:
                file_descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def write_text(
        self,
        raw_path: str,
        text: str,
        expected_version: str | None,
    ) -> dict:
        normalized, target = self._resolve(raw_path, allow_root=False)
        content = text.encode("utf-8")
        if len(content) > self.config.max_edit_size_bytes:
            raise FileTooLarge(
                f"File is too large to save ({len(content)} bytes; limit is "
                f"{self.config.max_edit_size_bytes})"
            )
        if not target.parent.is_dir():
            raise ManagedFileNotFound("Parent directory not found")

        existing_stat: os.stat_result | None = None
        try:
            if target.exists():
                if not target.is_file():
                    raise FileConflict("Requested path is not a file")
                existing_stat = target.stat()
                current = target.read_bytes()
                if expected_version is None:
                    raise FileConflict("File already exists; reopen it before saving")
                if not isinstance(expected_version, str) or not expected_version:
                    raise FileConflict("A valid file version is required")
                if not hashlib.sha256(current).hexdigest() == expected_version:
                    raise FileConflict(
                        "File changed on disk; reopen it before saving your changes"
                    )
            elif expected_version is not None:
                raise FileConflict("File no longer exists; refresh the directory")
            self._atomic_write(target, content, existing_stat)
        except FileManagerError:
            raise
        except OSError as exc:
            raise self._translate_os_error(exc, "Could not save file") from exc
        return {
            "path": normalized,
            "size": len(content),
            "version": _version(content),
        }

    def create_directory(self, raw_path: str) -> dict:
        normalized, target = self._resolve(raw_path, allow_root=False)
        if not target.parent.is_dir():
            raise ManagedFileNotFound("Parent directory not found")
        try:
            target.mkdir(mode=0o750)
        except FileExistsError as exc:
            raise FileConflict("A file or directory already exists at that path") from exc
        except OSError as exc:
            raise self._translate_os_error(exc, "Could not create directory") from exc
        return {"path": normalized, "created": True}

    def upload(self, raw_path: str, content: bytes, *, overwrite: bool) -> dict:
        normalized, target = self._resolve(raw_path, allow_root=False)
        if len(content) > self.config.max_upload_size_bytes:
            raise FileTooLarge(
                f"Upload is too large ({len(content)} bytes; limit is "
                f"{self.config.max_upload_size_bytes})"
            )
        if not target.parent.is_dir():
            raise ManagedFileNotFound("Parent directory not found")

        existing_stat: os.stat_result | None = None
        try:
            if target.exists():
                if not target.is_file():
                    raise FileConflict("A directory already exists at that path")
                if not overwrite:
                    raise FileConflict("File already exists; confirm overwrite to replace it")
                existing_stat = target.stat()
            self._atomic_write(target, content, existing_stat)
        except FileManagerError:
            raise
        except OSError as exc:
            raise self._translate_os_error(exc, "Could not upload file") from exc
        return {
            "path": normalized,
            "size": len(content),
            "version": _version(content),
        }
