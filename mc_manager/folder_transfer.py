"""Build bounded ZIP downloads using the existing authenticated agent file API."""
import asyncio
import tempfile
import zipfile
from pathlib import PurePosixPath

from .file_manager import ServerFileManager, FileManagerError

MAX_ARCHIVE_BYTES = 20 * 1024**3
MAX_ARCHIVE_ENTRIES = 100_000


async def build_folder_archive(agents, server, path):
    path = ServerFileManager.normalize_path(path)
    name = PurePosixPath(path).name if path else server.id
    handle = tempfile.TemporaryFile()
    total = 0
    count = 0
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
                                    await asyncio.to_thread(output.write, chunk)
                        finally:
                            await response.aclose()
        size = handle.tell()
        handle.seek(0)
        return handle, name + ".zip", size
    except BaseException:
        handle.close()
        raise
