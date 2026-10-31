"""Windows per-user DPAPI storage. Other platforms keep tokens in memory only."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from .instance import atomic_json, instance_root, read_json, safe_target


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; do not use machine-wide protection.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError("Windows could not protect/unlock the saved account. Sign out and sign in again.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class TokenStore:
    def __init__(self, directory: Path):
        self.root = instance_root(directory)

    @property
    def persistent(self):
        return os.name == "nt"

    def _path(self):
        return safe_target(self.root, "microsoft-account.json")

    def load(self, client_id: str) -> str | None:
        if not self.persistent or not self._path().exists():
            return None
        try:
            envelope = read_json(self._path())
            if envelope.get("protection") != "windows-dpapi-user-v1":
                raise ValueError()
            payload = json.loads(_dpapi(base64.b64decode(envelope["data"], validate=True), decrypt=True))
            if payload["client_id"] != client_id:
                return None
            token = payload["refresh_token"]
            if not isinstance(token, str) or not 1 <= len(token) <= 32768:
                raise ValueError()
            return token
        except (KeyError, TypeError, ValueError):
            raise ValueError("Saved Microsoft account could not be unlocked. Sign out, then sign in again.") from None

    def save(self, client_id: str, refresh_token: str):
        if not self.persistent:
            return
        raw = json.dumps({"client_id": client_id, "refresh_token": refresh_token}).encode()
        encrypted = _dpapi(raw)
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self._path(), {"protection": "windows-dpapi-user-v1",
                                  "data": base64.b64encode(encrypted).decode("ascii")})

    def clear(self):
        self._path().unlink(missing_ok=True)
