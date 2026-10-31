"""Manage client packs, sign in with Microsoft, install Minecraft, and play."""
from __future__ import annotations

import argparse
import json
import sys
import signal
from threading import Event
from pathlib import Path

import httpx

from .client import plan_installation, read_catalog
from .installer import install_release, recover_installation
from .auth import MicrosoftAccounts
from .credentials import TokenStore
from .game import install_client, play_game
from .library import inspect_installation
from .settings import default_data_directory, load_settings, save_settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", help="Catalog JSON path or complete HTTPS catalog URL")
    parser.add_argument("--allow-http", action="store_true", help="Allow HTTP for trusted development networks")
    parser.add_argument("--data-directory", type=Path, default=default_data_directory())
    parser.add_argument("--client-id", help="Registered public Microsoft application ID (never a secret)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List published profiles")
    plan = commands.add_parser("plan", help="Verify managed files and preview downloads; changes nothing")
    plan.add_argument("--profile", required=True)
    plan.add_argument("--instance", required=True, type=Path)
    install = commands.add_parser("install", help="Download, verify, and transactionally install managed pack files")
    install.add_argument("--profile", required=True)
    install.add_argument("--instance", required=True, type=Path)
    install.add_argument("--replace-modified", action="store_true", help="Back up and replace conflicting local files")
    recover = commands.add_parser("recover", help="Recover an interrupted installation without a catalog")
    recover.add_argument("--instance", required=True, type=Path)
    commands.add_parser("login", help="Sign in using the system browser")
    commands.add_parser("check-account", help="Retry the saved account and verify Minecraft access without opening a browser")
    commands.add_parser("logout", help="Remove the saved Microsoft account")
    status = commands.add_parser("status", help="Inspect installed release/runtime records without signing in or downloading")
    status.add_argument("--profile", required=True)
    status.add_argument("--instance", required=True, type=Path)
    status.add_argument("--java", default="", help="Absolute Java executable override")
    full = commands.add_parser("install-game", help="Install the pack, Minecraft, Java, and pinned loader")
    full.add_argument("--profile", required=True)
    full.add_argument("--instance", required=True, type=Path)
    full.add_argument("--replace-modified", action="store_true")
    full.add_argument("--java", default="", help="Absolute Java executable; default downloads official Java")
    play = commands.add_parser("play", help="Refresh the saved Microsoft account and run an installed client")
    play.add_argument("--profile", required=True)
    play.add_argument("--instance", required=True, type=Path)
    play.add_argument("--java", default="")
    play.add_argument("--memory", type=int, default=4096, help="Maximum Java heap in MiB")
    args = parser.parse_args(argv)
    if args.command not in {"recover", "login", "logout", "check-account"} and not args.catalog:
        parser.error("--catalog is required for catalog and game commands")
    cancel = Event()
    previous_signal = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        if args.command in {"login", "logout", "play", "check-account"}:
            settings = load_settings(args.data_directory.absolute())
            if args.client_id is not None:
                settings = type(settings).model_validate({**settings.model_dump(), "microsoft_client_id": args.client_id})
                save_settings(args.data_directory.absolute(), settings)
            accounts = MicrosoftAccounts(TokenStore(args.data_directory.absolute()))
            if args.command == "logout":
                accounts.sign_out()
                print("Signed out; saved account removed.")
                return 0
            if args.command == "login":
                print("Complete Microsoft sign-in in your browser.")
                session = accounts.sign_in(settings.microsoft_client_id, cancel=cancel)
                print(f"Signed in as {session.name}; Minecraft Java ownership verified.")
                if not accounts.store.persistent:
                    print("Account persistence is Windows-only; use the desktop to sign in and play in one session.")
                return 0
            if args.command == "check-account":
                session = accounts.refresh(settings.microsoft_client_id, cancel=cancel)
                print(f"Signed in as {session.name}; Minecraft Java ownership verified.")
                return 0
        if args.command == "recover":
            print(json.dumps(recover_installation(args.instance), indent=2))
            return 0
        catalog = read_catalog(args.catalog, allow_http=args.allow_http)
        if args.command == "list":
            result = [{"id": item.id, "name": item.name, "version": item.release.version,
                       "minecraft_version": item.release.minecraft_version, "loader": item.release.loader.kind}
                      for item in catalog.profiles]
        else:
            profile = next((item for item in catalog.profiles if item.id == args.profile), None)
            if profile is None:
                raise ValueError("Profile is not in the published catalog")
            if args.command == "status":
                result = {"profile_id": profile.id, "published_version": profile.release.version,
                          **inspect_installation(profile.release, args.instance, java=args.java).to_dict()}
            elif args.command in {"install", "install-game"}:
                installer = install_client if args.command == "install-game" else install_release
                extra = {"java": args.java} if args.command == "install-game" else {}
                result = installer(profile.release, args.instance, replace_modified=args.replace_modified, cancel=cancel, **extra)
            elif args.command == "play":
                result = play_game(profile.release, args.instance, accounts, settings.microsoft_client_id,
                                   memory_mb=args.memory, java=args.java, cancel=cancel)
                print(json.dumps(result, indent=2))
                return 0 if result["exit_code"] == 0 else 1
            else:
                result = plan_installation(profile.release, args.instance)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"Launcher: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_signal)


if __name__ == "__main__":
    raise SystemExit(main())
