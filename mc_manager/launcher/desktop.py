"""Desktop launcher for panel packs, Microsoft accounts, and Minecraft Java."""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .client import plan_installation, read_catalog
from .branding import set_process_identity, set_window_icon
from .auth import AuthenticationError, MinecraftAccessDenied, MicrosoftAccounts, SignInRequired
from .credentials import TokenStore
from .game import install_client, play_game
from .installer import InstallCancelled, recover_installation
from .instance import safe_target
from .library import inspect_library
from .models import Catalog
from .settings import DesktopSettings, default_data_directory, load_settings, profile_directory, save_settings


class LauncherWindow:
    def __init__(self, root: tk.Tk, data_directory: Path, catalog: str | None = None):
        self.root = root
        self.data_directory = data_directory
        self.settings = load_settings(data_directory)
        self.accounts = MicrosoftAccounts(TokenStore(data_directory))
        self.playing = False
        self.catalog = Catalog()
        self.installations = {}
        self.saved_account = False
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.closing = False
        self.worker = None
        self.source = tk.StringVar(value=catalog or self.settings.catalog_source)
        self.library = tk.StringVar(value=self.settings.library_directory)
        self.memory = tk.StringVar(value=str(self.settings.memory_mb))
        self.allow_http = tk.BooleanVar(value=self.settings.allow_http)
        self.client_id = tk.StringVar(value=self.settings.microsoft_client_id)
        self.java = tk.StringVar(value=self.settings.java_executable)
        self.account = tk.StringVar(value="Sign in to play Minecraft Java Edition")
        self.status = tk.StringVar(value="Load a catalog exported or published by your server panel.")
        self.details = tk.StringVar(value="Select a client profile")
        self.instance = tk.StringVar(value="")
        self.library_summary = tk.StringVar(value="No catalog loaded")
        self.controls = []
        self._build()
        self._saved_account_status()
        self._sync_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._poll)
        if self.source.get():
            self.root.after(100, self.refresh)

    def _build(self):
        from .view import build_window
        build_window(self)

    def _button(self, parent, text, command, primary=False):
        button = ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")
        self.controls.append(button)
        return button

    def _render_details(self, *args):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", self.details.get())
        self.detail_text.tag_add("title", "1.0", "1.end")
        self.detail_text.configure(state="disabled")

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _profile(self):
        selected = self.profiles.selection()
        return next((profile for profile in self.catalog.profiles if selected and profile.id == selected[0]), None)

    def _selection(self, event=None):
        profile = self._profile()
        self.pack_art.set_profile(profile)
        if not profile:
            self.details.set("No published profiles in this catalog.")
            self.instance.set("")
            self._sync_buttons()
            return
        release = profile.release
        local = self.installations.get(profile.id)
        installation = (f"On this PC: {local.label} · Installed: {local.installed_version or 'None'}\n{local.message}"
                        if local else "On this PC: status not checked. Use Refresh status.")
        loader = release.loader.kind + (f" {release.loader.version}" if release.loader.version else "")
        self.details.set(f"{profile.name}\n\n{installation}\n\nPublished release {release.version}\nMinecraft {release.minecraft_version} · {loader}\n"
                         f"Java {release.java_major} · Recommended {release.memory_mb} MiB\n{len(release.files)} pack files\n"
                         f"Server: {release.server_address or 'Not specified'}\n\n{profile.description}\n\n{release.changelog}")
        try:
            self.instance.set("Instance folder: " + profile_directory(self._preferences(), profile.id).name)
        except ValueError:
            self.instance.set("Choose an absolute instance library directory.")
        self._sync_buttons()

    def _sync_buttons(self):
        if self.busy:
            return
        profile = self._profile()
        local = self.installations.get(profile.id) if profile else None
        self.retry_account_button.state(["!disabled" if self.saved_account else "disabled"])
        self.install_button.configure(text="Update" if local and local.state == "release_changed" else "Install / repair")
        self.install_button.state(["!disabled" if profile and (not local or local.state not in {"recover", "blocked"}) else "disabled"])
        self.play_button.state(["!disabled" if profile and local and local.state == "installed" else "disabled"])
        self.recover_button.state(["!disabled" if profile and local and local.state == "recover" else "disabled"])
        self.folder_button.state(["!disabled" if profile else "disabled"])
        self.log_button.state(["!disabled" if profile else "disabled"])
        self.server_button.state(["!disabled" if profile and profile.release.server_address else "disabled"])

    def _set_installations(self, statuses):
        self.installations = statuses
        for profile in self.catalog.profiles:
            local = statuses.get(profile.id)
            self.profiles.item(profile.id, values=(profile.release.version, local.label if local else "Not checked"),
                               tags=(local.state,) if local else ())
        installed = sum(item.state == "installed" for item in statuses.values())
        changed = sum(item.state == "release_changed" for item in statuses.values())
        attention = sum(item.state in {"repair", "recover", "blocked"} for item in statuses.values())
        self.library_summary.set(f"{installed} installed · {changed} release changes · {attention} need attention")
        self._selection()

    def _saved_account_status(self):
        try:
            self.saved_account = bool(self.client_id.get() and self.accounts.has_saved_account(self.client_id.get()))
            self.account.set("Microsoft account saved — retry to check Minecraft access" if self.saved_account else "Sign in to play Minecraft Java Edition")
        except (OSError, ValueError):
            self.saved_account = False
            self.account.set("Saved account needs attention. Sign out, then sign in again.")

    def _preferences(self):
        profile = self._profile()
        return DesktopSettings(catalog_source=self.source.get().strip(), library_directory=self.library.get().strip(),
                               allow_http=self.allow_http.get(), memory_mb=int(self.memory.get()),
                               microsoft_client_id=self.client_id.get().strip(), java_executable=self.java.get().strip(),
                               selected_profile=profile.id if profile else self.settings.selected_profile)

    def persist(self):
        try:
            settings = self._preferences()
            save_settings(self.data_directory, settings)
            changed_library = (settings.library_directory, settings.java_executable) != (self.settings.library_directory, self.settings.java_executable)
            self.settings = settings
            if changed_library:
                self._set_installations({})
                self.library_summary.set("Library changed — refresh status")
            self._selection()
            self.status.set("Settings saved.")
            return True
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))
            messagebox.showerror("Settings", str(exc), parent=self.root)
            return False

    def browse_catalog(self):
        path = filedialog.askopenfilename(parent=self.root, title="Open launcher catalog", filetypes=[("JSON catalogs", "*.json")])
        if path:
            self.source.set(path)
            self.refresh()

    def browse_library(self):
        path = filedialog.askdirectory(parent=self.root, title="Choose instance library")
        if path:
            self.library.set(path)
            self.save_and_scan()

    def save_and_scan(self):
        if not self.busy and self.persist():
            self.rescan(save=False)

    def rescan(self, *, save=True):
        if self.busy or (save and not self.persist()):
            return
        catalog, settings = self.catalog, self.settings
        self.status.set("Checking installed release and runtime records…")
        def completed(statuses):
            self._set_installations(statuses)
            self.status.set("Library status refreshed. Check files verifies pack contents; Play verifies Minecraft access.")
        self._work(lambda: inspect_library(catalog, settings, cancel=self.cancel), completed)

    def open_instance(self):
        target = self._target()
        if not target:
            return
        path = target[1]
        try:
            if not path.is_dir():
                raise ValueError("This profile has no instance folder yet. Install it first.")
            if os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open instance", str(exc), parent=self.root)

    def copy_server(self):
        profile = self._profile()
        if profile and profile.release.server_address:
            self.root.clipboard_clear()
            self.root.clipboard_append(profile.release.server_address)
            self.status.set("Server address copied. Paste it in Minecraft's Multiplayer screen.")

    def show_game_log(self):
        target = self._target()
        if not target:
            return
        try:
            path = safe_target(target[1], ".launcher/game-output.log")
            if not path.is_file():
                raise ValueError("No game log yet. A log is created when this launcher starts Minecraft.")
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 256 * 1024))
                content = handle.read(256 * 1024).decode("utf-8", errors="replace")
            if size > 256 * 1024:
                content = "[Showing the last 256 KiB]\n" + content.partition("\n")[2]
        except (OSError, ValueError) as exc:
            messagebox.showerror("Game log", str(exc), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Game log — {target[0].name}")
        dialog.geometry("900x550")
        dialog.transient(self.root)
        text = tk.Text(dialog, wrap="word", background="#111a15", foreground="#b9d4c4", font=("Consolas", 10))
        scroll = ttk.Scrollbar(dialog, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")
        text.see("end")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def account_settings(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Microsoft account and Java settings")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Microsoft application client ID", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        client = tk.StringVar(value=self.client_id.get())
        java = tk.StringVar(value=self.java.get())
        ttk.Entry(frame, textvariable=client, width=66).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 10))
        ttk.Label(frame, text="Register a desktop app supporting personal Microsoft accounts with redirect URI http://localhost.\n"
                  "Use the public client ID here. Minecraft API approval may also be required.\n"
                  "Passwords are entered only in your browser; no client secret is needed.", wraplength=620).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(frame, text="Open Microsoft app registrations", command=lambda: webbrowser.open("https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade")).grid(row=3, column=0, sticky="w", pady=10)
        ttk.Button(frame, text="Minecraft API application", command=lambda: webbrowser.open("https://aka.ms/mce-reviewappid")).grid(row=3, column=1, sticky="e", pady=10)
        ttk.Label(frame, text="Java executable (blank = automatic download)").grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 8))
        ttk.Entry(frame, textvariable=java, width=66).grid(row=5, column=0, sticky="ew")
        def choose_java():
            path = filedialog.askopenfilename(parent=dialog, title="Choose java.exe or java executable")
            if path:
                java.set(path)
        ttk.Button(frame, text="Browse…", command=choose_java).grid(row=5, column=1, padx=(8, 0))
        storage = ("Windows encrypts the saved refresh token for your current user. Sign out removes it."
                   if self.accounts.store.persistent else "On this platform, account tokens last only for this launcher session.")
        ttk.Label(frame, text=storage, wraplength=620, style="Muted.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=16)
        def save():
            before = self.client_id.get(), self.java.get()
            self.client_id.set(client.get().strip())
            self.java.set(java.get().strip())
            if self.persist():
                if before[0] != self.client_id.get():
                    self.accounts.session = None
                    self._saved_account_status()
                dialog.destroy()
                self.rescan(save=False)
            else:
                self.client_id.set(before[0])
                self.java.set(before[1])
        ttk.Button(frame, text="Save", command=save).grid(row=7, column=1, sticky="e")
        dialog.update_idletasks()
        dialog.geometry(f"+{max(0, self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2)}"
                        f"+{max(0, self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2)}")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def sign_in(self):
        if self.busy or not self.persist():
            return
        if not self.settings.microsoft_client_id:
            self.status.set("Add your Microsoft application client ID, then sign in.")
            self.account_settings()
            return
        client_id = self.settings.microsoft_client_id
        self.status.set("Complete sign-in in your browser. Waiting up to five minutes…")
        self._work(lambda: self.accounts.sign_in(client_id, cancel=self.cancel), self._account_ready)

    def _account_ready(self, session):
        self.saved_account = True
        self.account.set(f"Signed in as {session.name}")
        self.status.set("Minecraft Java Edition ownership verified. Install a profile, then press Play.")
        self._log(self.status.get())

    def retry_account(self):
        if self.busy or not self.persist():
            return
        client_id = self.settings.microsoft_client_id
        if not client_id:
            self.account_settings()
            return
        self.status.set("Retrying the saved Microsoft account and checking Minecraft access…")
        self._work(lambda: self.accounts.refresh(client_id, cancel=self.cancel), self._account_ready)

    def sign_out(self):
        if self.busy:
            return
        try:
            self.accounts.sign_out()
            self.saved_account = False
            self.account.set("Signed out")
            self.status.set("Saved account removed from this launcher.")
            self._sync_buttons()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Sign out", str(exc), parent=self.root)

    def cancel_operation(self):
        if self.playing and not messagebox.askyesno("Stop Minecraft", "Force Minecraft to stop? Unsaved game progress may be lost.", parent=self.root):
            return
        self.cancel.set()

    def _work(self, operation, finished, *, cancel_before_result=True, refresh_library=False):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        for control in self.controls:
            control.state(["disabled"])
        self.profiles.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        self.progress.configure(value=0)
        catalog, settings = self.catalog, self.settings
        def worker():
            try:
                result = operation()
                if cancel_before_result and self.cancel.is_set():
                    raise InstallCancelled("Operation cancelled.")
                outcome = ("result", (finished, result))
            except Exception as exc:
                outcome = ("error", exc)
            if refresh_library:
                try:
                    self.events.put(("library", inspect_library(catalog, settings)))
                except (OSError, ValueError):
                    pass  # Preserve the operation's original result/error.
            self.events.put(outcome)
        self.worker = threading.Thread(target=worker, daemon=False)
        self.worker.start()

    def _progress_callback(self):
        last = [0.0, ""]
        def update(event):
            now = time.monotonic()
            if now - last[0] >= 0.1 or event["phase"] != last[1]:
                self.events.put(("progress", event))
                last[:] = [now, event["phase"]]
        return update

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "library":
                    self._set_installations(value)
                    continue
                if kind == "progress":
                    if value["phase"] == "playing":
                        self.playing = True
                        self.cancel_button.configure(text="Stop game")
                        if self.accounts.session:
                            self.account.set(f"Signed in as {self.accounts.session.name}")
                    self.status.set(value.get("message") or f"{value['phase'].capitalize()}: {value.get('path', '')}")
                    self.progress.configure(value=100 * value.get("completed", 0) / max(1, value.get("total", 1)))
                    continue
                self.busy = False
                self.playing = False
                self.cancel_button.configure(text="Cancel")
                for control in self.controls:
                    control.state(["!disabled"])
                self.profiles.state(["!disabled"])
                self.cancel_button.state(["disabled"])
                if self.closing:
                    self.root.destroy()
                    return
                if kind == "error":
                    if isinstance(value, AuthenticationError):
                        self._saved_account_status()
                        if isinstance(value, MinecraftAccessDenied):
                            self.account.set("Microsoft account saved · Minecraft app access rejected")
                        elif isinstance(value, SignInRequired):
                            self.account.set("Sign in with Microsoft to reconnect your account")
                        else:
                            self.account.set("Sign-in needs attention")
                    self.status.set(str(value))
                    self._log(str(value))
                    if not isinstance(value, InstallCancelled):
                        messagebox.showerror("Launcher", str(value), parent=self.root)
                else:
                    callback, result = value
                    callback(result)
                self._sync_buttons()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def refresh(self):
        if self.busy or not self.persist():
            return
        source, allow_http = self.settings.catalog_source, self.settings.allow_http
        if not source:
            self.status.set("Enter a catalog URL or choose an exported JSON file.")
            return
        self.status.set("Loading catalog…")
        settings = self.settings
        def load():
            catalog = read_catalog(source, allow_http=allow_http)
            return catalog, inspect_library(catalog, settings, cancel=self.cancel)
        def loaded(result):
            catalog, statuses = result
            self.catalog = catalog
            self.profiles.delete(*self.profiles.get_children())
            for profile in catalog.profiles:
                self.profiles.insert("", "end", iid=profile.id, text=profile.name,
                                     image=self.artwork.thumbnail(profile), values=(profile.release.version, "Checking…"))
            ids = [profile.id for profile in catalog.profiles]
            if ids:
                self.profiles.selection_set(self.settings.selected_profile if self.settings.selected_profile in ids else ids[0])
            self._set_installations(statuses)
            self.status.set(f"Loaded {len(ids)} published profiles.")
            self._log(self.status.get())
        self._work(load, loaded)

    def _target(self):
        if self.busy:
            return None
        profile = self._profile()
        if not profile:
            self.status.set("Load a catalog and select a profile first.")
            return None
        if not self.persist():
            return None
        try:
            return profile, profile_directory(self.settings, profile.id)
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))
            messagebox.showerror("Instance folder", str(exc), parent=self.root)
            return None

    def _describe_plan(self, plan):
        downloads = sum(file["action"] != "keep" for file in plan["files"])
        self.status.set(f"{downloads} downloads · {len(plan['removals'])} obsolete managed files · {plan['download_bytes']:,} bytes")
        self._log(self.status.get())
        for item in [*plan["files"], *plan["removals"]]:
            if item["action"] != "keep":
                self._log(f"{item['action']}: {item['path']}" + (" (local changes need review)" if item["conflict"] else ""))

    def check(self):
        target = self._target()
        if target:
            profile, path = target
            self.status.set("Checking file sizes and hashes…")
            self._work(lambda: plan_installation(profile.release, path), self._describe_plan, refresh_library=True)

    def install(self):
        target = self._target()
        if not target:
            return
        profile, path = target
        def reviewed(plan):
            self._describe_plan(plan)
            conflicts = plan["conflicts"]
            if conflicts and not messagebox.askyesno("Preserve local changes?",
                "These files differ from the installed pack or belong to you:\n\n" + "\n".join(conflicts[:12]) +
                (f"\n…and {len(conflicts) - 12} more (see log)." if len(conflicts) > 12 else "") +
                "\n\nReplace/remove them after saving recovery backups?", parent=self.root):
                self.status.set("Installation cancelled. Local files were preserved.")
                return
            progress = self._progress_callback()
            def completed(result):
                self.progress.configure(value=100)
                self.status.set(f"Minecraft and pack release {result['version']} installed. Sign in and press Play.")
                self._log(self.status.get())
                self._log("Recovery backups: " + result["backup_directory"])
            java = self.settings.java_executable
            self._work(lambda: install_client(profile.release, path, java=java, replace_modified=bool(conflicts),
                expected_plan_id=plan["plan_id"], cancel=self.cancel, progress=progress), completed, cancel_before_result=False, refresh_library=True)
        self.status.set("Preparing installation preview…")
        self._work(lambda: plan_installation(profile.release, path), reviewed, refresh_library=True)

    def play(self):
        target = self._target()
        if not target:
            return
        if not self.settings.microsoft_client_id:
            self.status.set("Configure your Microsoft application and sign in before playing.")
            self.account_settings()
            return
        profile, path = target
        settings = self.settings
        self.status.set("Checking the installed client and refreshing your Microsoft session…")
        def finished(result):
            if result["stopped"]:
                self.status.set("Minecraft stopped.")
            elif result["exit_code"] == 0:
                self.status.set("Minecraft closed.")
            else:
                self.status.set(f"Minecraft exited with code {result['exit_code']}. See {result['log']}")
            self._log(self.status.get())
            self._log("Game output: " + result["log"])
        self._work(lambda: play_game(profile.release, path, self.accounts, settings.microsoft_client_id,
            memory_mb=settings.memory_mb, java=settings.java_executable, cancel=self.cancel,
            progress=self._progress_callback()), finished, cancel_before_result=False, refresh_library=True)

    def recover(self):
        target = self._target()
        if target:
            def completed(result):
                self.status.set("Interrupted installation recovered." if result["recovered"] else "No interrupted installation found.")
                self._log(self.status.get())
            self._work(lambda: recover_installation(target[1]), completed, cancel_before_result=False, refresh_library=True)

    def close(self):
        if self.busy:
            prompt = ("Stop Minecraft and close the launcher? Unsaved game progress may be lost." if self.playing else
                      "Cancel the current operation and close after it finishes safely?")
            if messagebox.askyesno("Close launcher", prompt, parent=self.root):
                self.closing = True
                self.cancel.set()
                self.status.set("Waiting for the current operation to finish safely…")
        else:
            self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the Monkeycraft desktop launcher")
    parser.add_argument("--catalog", help="Initial catalog URL or exported JSON file")
    parser.add_argument("--data-directory", type=Path, default=default_data_directory())
    args = parser.parse_args(argv)
    set_process_identity()
    root = tk.Tk()
    set_window_icon(root)
    try:
        LauncherWindow(root, args.data_directory.absolute(), args.catalog)
    except (OSError, ValueError) as exc:
        messagebox.showerror("Launcher settings", str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
