"""Bundled launcher artwork and responsive Tk presentation; no remote fetches."""
from __future__ import annotations

from pathlib import Path
import tkinter as tk

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # Keep the launcher usable before the optional extra is installed.
    Image = ImageOps = ImageTk = None

ASSETS = Path(__file__).with_name("assets")


def profile_art(profile):
    name = f"{profile.id} {profile.name}".casefold()
    if "civilization" in name:
        return "civilizations.png"
    if profile.release.loader.kind == "vanilla":
        return "vanilla.png"
    if any(word in name for word in ("create", "world", "adventure")):
        return "overworld.png"
    return "civilizations.png"


class Artwork:
    def __init__(self, root):
        self.root = root
        self.originals = {}
        self.thumbnails = {}

    def load(self, name):
        if name not in self.originals:
            try:
                if Image is None:
                    return None
                with Image.open(ASSETS / name) as image:
                    self.originals[name] = image.convert("RGB")
            except OSError:
                self.originals[name] = None
        return self.originals[name]

    def thumbnail(self, profile):
        name = profile_art(profile)
        if name not in self.thumbnails:
            source = self.load(name)
            self.thumbnails[name] = (ImageTk.PhotoImage(ImageOps.fit(source, (64, 46), centering=(.5, .3)), master=self.root)
                                     if source else "")
        return self.thumbnails[name]


class ArtCanvas(tk.Canvas):
    def __init__(self, parent, artwork, *, banner=False, **kwargs):
        super().__init__(parent, background="#1a2417", highlightthickness=0, **kwargs)
        self.artwork, self.banner = artwork, banner
        self.asset = "overworld.png"
        self.badge = "YOUR NEXT ADVENTURE"
        self.photo = None
        self.pending = None
        self.bind("<Configure>", self._schedule)
        self.bind("<Destroy>", self._destroy, add="+")

    def _destroy(self, event):
        if event.widget == self and self.pending:
            self.after_cancel(self.pending)
            self.pending = None

    def _schedule(self, _event=None):
        if self.pending:
            self.after_cancel(self.pending)
        self.pending = self.after(60, self._draw)

    def set_profile(self, profile):
        self.asset = profile_art(profile) if profile else "overworld.png"
        self.badge = ("VANILLA" if profile.release.loader.kind == "vanilla" else profile.release.loader.kind.upper()) if profile else "YOUR NEXT ADVENTURE"
        self._schedule()

    def _draw(self):
        self.pending = None
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        self.delete("art")
        source = self.artwork.load(self.asset)
        if source is not None:
            fitted = ImageOps.fit(source, (min(width, 4096), min(height, 2048)),
                                  centering=(.5, .32 if self.banner else .22))
            self.photo = ImageTk.PhotoImage(fitted, master=self)
            self.create_image(0, 0, image=self.photo, anchor="nw", tags="art")
        if self.banner:
            self.create_rectangle(0, 0, 510, height, fill="#10180e", stipple="gray50", outline="", tags="art")
            for offset, color in [(3, "#11170d"), (0, "#f2f4dc")]:
                self.create_text(28 + offset, 35 + offset, text="MONKEYCRAFT", fill=color,
                                 font=("Impact", 36), anchor="nw", tags="art")
            self.create_text(31, 101, text="BUILD. EXPLORE. MAKE IT YOUR WORLD.", fill="#d5e9ad",
                             font=("Consolas", 11, "bold"), anchor="nw", tags="art")
            self.create_text(31, 132, text="JAVA EDITION  /  COMMUNITY LAUNCHER", fill="#f0d8a4",
                             font=("Consolas", 9), anchor="nw", tags="art")
        else:
            self.create_rectangle(16, 16, 24 + len(self.badge) * 9, 43, fill="#1d2b18", outline="#9eb478", tags="art")
            self.create_text(23, 22, text=self.badge, fill="#e8f4d1", font=("Consolas", 10, "bold"), anchor="nw", tags="art")
        # A grass-green top edge and dark lower edge echo Minecraft's bevels.
        self.create_rectangle(0, 0, width, 3, fill="#93b85b", outline="", tags="art")
        self.create_rectangle(0, height - 3, width, height, fill="#0c120a", outline="", tags="art")
        self.tag_lower("art")
