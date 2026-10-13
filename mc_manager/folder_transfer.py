"""Build bounded ZIP downloads using the existing authenticated agent file API."""
import asyncio
import errno
import shutil
import tempfile
import zipfile
from pathlib import PurePosixPath

from .file_manager import ServerFileManager, FileManagerError

MAX_ARCHIVE_BYTES = 20 * 1024**3
MAX_ARCHIVE_ENTRIES = 100_000
DISK_RESERVE_BYTES = 512 * 1024**2


class ArchiveStorageError(FileManagerError):
    status_code = 507


def check_archive_space(directory, required=0):
    if shutil.disk_usage(directory).free < required + DISK_RESERVE_BYTES:
        raise ArchiveStorageError("Not enough disk space to prepare this folder download")


async def build_folder_archive(agents, server, path, *, directory=None):
    path = ServerFileManager.normalize_path(path)
    name = PurePosixPath(path).name if path else server.id
    storage_directory = directory or tempfile.gettempdir()
    check_archive_space(storage_directory)
    handle = tempfile.TemporaryFile(dir=storage_directory)
    total = 0
    count = 0
    next_space_check = 0
    pending = [(path, name, 0)]
    try:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            while pending:
                directory, archive_path, depth = pending.pop()
                if depth > 64:
                    raise FileManagerError("Folder nesting exceeds 64 levels")
                listing = await agents.files(server, directory)
                archive.writestr(archive_path + "/", b"")
                for entry in listing["entries"]:
                    count += 1
                    if count > MAX_ARCHIVE_ENTRIES:
                        raise FileManagerError("Folder exceeds 100,000 entries")
                    child = entry["name"]
                    if not child or child in {".", ".."} or "/" in child or "\\" in child:
                        raise FileManagerError("Invalid folder entry")
                    remote = ServerFileManager.normalize_path(f"{directory}/{child}" if directory else child)
                    destination = f"{archive_path}/{child}"
                    if entry["kind"] == "directory":
                        pending.append((remote, destination, depth + 1))
                    elif entry["kind"] == "file":
                        response = await agents.download_file(server, remote)
                        try:
                            with archive.open(destination, "w", force_zip64=True) as output:
                                async for chunk in response.aiter_bytes():
                                    total += len(chunk)
                                    if total > MAX_ARCHIVE_BYTES:
                                        raise FileManagerError("Folder download exceeds the 20 GiB limit")
                                    if total >= next_space_check:
                                        check_archive_space(storage_directory, 64 * 1024**2)
                                        next_space_check = total + 64 * 1024**2
                                    await asyncio.to_thread(output.write, chunk)
                        finally:
                            await response.aclose()
        size = handle.tell()
        handle.seek(0)
        return handle, name + ".zip", size
    except BaseException as exc:
        handle.close()
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise ArchiveStorageError("Not enough disk space to prepare this folder download") from exc
        raise
