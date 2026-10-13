import io
from pathlib import Path
from unittest.mock import Mock

import pytest

from mc_manager.file_manager import ServerFileManager, FileAccessDenied
from mc_manager.config import FileManagerConfig


def manager(monkeypatch, tmp_path, stream):
    manager = ServerFileManager(FileManagerConfig(root=tmp_path))
    manager.root = Path('/srv/minecraft/survival')
    monkeypatch.setattr(manager, '_resolve', lambda *a, **k: ('world/level.dat', tmp_path / 'level.dat'))
    monkeypatch.setattr('mc_manager.file_manager.os.open', Mock(side_effect=PermissionError()))
    monkeypatch.setattr(Path, 'is_file', lambda p: True)
    process = Mock(stdout=io.BytesIO(stream))
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr('mc_manager.file_manager.subprocess.Popen', launch)
    return manager, process, launch


def test_owner_only_download_uses_restricted_owner_helper(monkeypatch, tmp_path):
    files, process, launch = manager(monkeypatch, tmp_path, b'4\ndata')
    result = files.open_download('world/level.dat')
    assert result.size == 4
    assert b''.join(result.iter_chunks()) == b'data'
    assert launch.call_args.args[0] == ['sudo', '-n', '-u', 'minecraft', '/usr/local/sbin/read-minecraft-file', 'survival', 'world/level.dat']
    process.terminate.assert_called_once()
    process.wait.assert_called_once()


def test_owner_helper_denial_closes_process(monkeypatch, tmp_path):
    files, process, launch = manager(monkeypatch, tmp_path, b'')
    with pytest.raises(FileAccessDenied):
        files.open_download('world/level.dat')
    assert process.stdout.closed
    process.wait.assert_called_once()
