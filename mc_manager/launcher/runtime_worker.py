"""Isolated, cancellable minecraft-launcher-lib installer (no account tokens)."""
from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .game import audit_runtime, java_major, launcher_library, runtime_directory
from .instance import instance_root, read_json, safe_target
from .models import ReleaseManifest


def emit(value):
    print("MONKEYCRAFT:" + json.dumps(value), flush=True)


def configure_network():
    """This process owns these hooks; never patch networking in the panel/UI."""
    import requests
    original_send = requests.sessions.Session.send
    original_request = requests.sessions.Session.request

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            raise ValueError("Minecraft downloads require HTTPS without embedded credentials")
        kwargs.setdefault("timeout", (10, 45))
        if kwargs["timeout"] is None:
            kwargs["timeout"] = (10, 45)
        return original_send(self, request, **kwargs)

    def request(self, *args, **kwargs):
        response = original_request(self, *args, **kwargs)
        response.raise_for_status()
        return response

    requests.sessions.Session.send = send
    requests.sessions.Session.request = request


@contextmanager
def custom_java_override(lib, enabled):
    """Restore the pinned library's automatic-Java hook even after failure."""
    if not enabled:
        yield
        return
    original = lib.install.install_jvm_runtime
    lib.install.install_jvm_runtime = lambda *args, **kwargs: None
    try:
        yield
    finally:
        lib.install.install_jvm_runtime = original


def install_runtime(manifest, root, java="", *, progress=None):
    lib = launcher_library()
    runtime = runtime_directory(root)
    runtime.mkdir(parents=True, exist_ok=True)
    state = {"message": "Preparing Minecraft", "completed": 0, "total": 1}
    last = [0.0]
    lock = threading.Lock()
    def update(**values):
        with lock:
            changed = "message" in values and values["message"] != state["message"]
            state.update(values)
            now = time.monotonic()
            if progress and (changed or now - last[0] >= 0.15):
                progress({"phase": "minecraft", **state})
                last[0] = now
    callbacks = {"setStatus": lambda text: update(message=str(text)),
                 "setProgress": lambda value: update(completed=value),
                 "setMax": lambda value: update(completed=0, total=max(1, value))}
    if java and java_major(java) != manifest.java_major:
        raise ValueError(f"The selected executable must be Java {manifest.java_major}.")
    # The pinned library automatically downloads Java even with an override.
    # Suppress that optional step in this isolated worker so custom Java works
    # on platforms where Mojang does not provide a runtime.
    with custom_java_override(lib, bool(java)):
        lib.install.install_minecraft_version(manifest.minecraft_version, runtime, callback=callbacks)
        metadata = read_json(safe_target(runtime, f"versions/{manifest.minecraft_version}/{manifest.minecraft_version}.json"))
        official_major = metadata.get("javaVersion", {}).get("majorVersion", 8)
        if manifest.java_major < official_major:
            raise ValueError(f"Minecraft {manifest.minecraft_version} requires at least Java {official_major}; update the panel release.")
        if not java:
            component = metadata.get("javaVersion", {}).get("component", "jre-legacy")
            if not metadata.get("javaVersion"):
                lib.runtime.install_jvm_runtime(component, runtime, callback=callbacks)
            java = lib.runtime.get_executable_path(component, runtime)
            if not java:
                raise ValueError("Automatic Java is unavailable on this platform. Choose a Java executable in Settings.")
        if java_major(java) != manifest.java_major:
            raise ValueError(f"This release requires Java {manifest.java_major}. Select that Java executable or correct the panel release.")
        version_id = manifest.minecraft_version
        if manifest.loader.kind != "vanilla":
            loader = lib.mod_loader.get_mod_loader(manifest.loader.kind)
            loader_version = manifest.loader.version
            # Forge's unified API accepts the loader part, e.g. 52.0.28, without 1.21.1-.
            if loader_version not in loader.get_loader_versions(manifest.minecraft_version, False):
                raise ValueError(f"{manifest.loader.kind} {loader_version} is not available for Minecraft {manifest.minecraft_version}.")
            version_id = loader.install(manifest.minecraft_version, runtime, loader_version=loader_version,
                                        callback=callbacks, java=java)
    audit_runtime(runtime)
    metadata_path = safe_target(runtime, f"versions/{version_id}/{version_id}.json")
    if not metadata_path.is_file():
        raise ValueError("The Minecraft installer did not produce version metadata.")
    update(message="Minecraft and Java are installed", completed=1, total=1)
    return {"version_id": version_id, "java": str(Path(java).absolute())}


def main():
    try:
        request = json.loads(sys.stdin.readline(16 * 1024 * 1024))
        manifest = ReleaseManifest.model_validate(request["manifest"])
        root = instance_root(Path(request["root"]))
        configure_network()
        result = install_runtime(manifest, root, request.get("java", ""),
                                 progress=lambda event: emit({"type": "progress", "event": event}))
        emit({"type": "result", "result": result})
        return 0
    except Exception as exc:
        emit({"type": "error", "message": f"Minecraft installation failed ({type(exc).__name__}): {exc}"[:1600]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
