"""Public desktop executable entry point; contains no controller routes."""
import json
import sys
from importlib.metadata import version
from pathlib import Path


def main():
    if sys.argv[1:] == ["--runtime-worker"]:
        from mc_manager.launcher.runtime_worker import main as worker_main
        return worker_main()
    if sys.argv[1:] == ["--self-check"]:
        from mc_manager.launcher.branding import ASSETS
        from mc_manager.launcher.game import launcher_library
        launcher_library()
        required = ["launcher.ico", "launcher-icon.png", "overworld.png", "civilizations.png", "vanilla.png", "stone.png"]
        assert all((ASSETS / name).is_file() for name in required)
        print(json.dumps({"ok": True, "launcher_library": version("minecraft-launcher-lib"), "assets": required}))
        return 0
    from mc_manager.launcher.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
