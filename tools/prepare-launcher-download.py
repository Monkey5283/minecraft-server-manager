"""Create a static, checksum-verifiable download without publishing private files."""
import argparse
import hashlib
import json
from pathlib import Path


def prepare(binary: Path, destination: Path, version: str):
    destination = destination.resolve()
    parts = destination / "downloads"
    parts.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    manifest_parts = []
    with binary.open("rb") as source:
        number = 0
        while block := source.read(16 * 1024 * 1024):
            total += len(block)
            if total > 100 * 1024 * 1024:
                raise ValueError("Download exceeds the supported browser verification limit")
            digest.update(block)
            sha = hashlib.sha256(block).hexdigest()
            name = f"launcher-{sha[:16]}-{number}.bin"
            (parts / name).write_bytes(block)
            manifest_parts.append({"path": "/downloads/" + name, "size": len(block), "sha256": sha})
            number += 1
    if not total:
        raise ValueError("Empty launcher executable")
    manifest = {"version": version, "filename": "Monkeycraft-Launcher-Windows-x64.exe", "size": total,
                "sha256": digest.hexdigest(), "channel": "preview", "signed": False,
                "minecraft_api_approval": "pending", "parts": manifest_parts}
    (destination / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (destination / "SHA256SUMS.txt").write_text(f"{manifest['sha256']}  {manifest['filename']}\n", encoding="ascii")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--version", default="0.1.0-preview.1")
    args = parser.parse_args()
    prepare(args.binary, args.destination, args.version)
