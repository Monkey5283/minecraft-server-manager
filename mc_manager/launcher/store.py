"""Transactional, immutable releases with an explicit current-version pointer."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Catalog, CatalogProfile, NewProfile, ProfileUpdate, ReleaseManifest, ReleaseSpec


class LauncherStoreError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LauncherStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self, *, write=False):
        # Lazy initialization avoids creating state merely by importing/building the app.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, settings TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, active_version TEXT
                );
                CREATE TABLE IF NOT EXISTS releases (
                    profile_id TEXT NOT NULL REFERENCES profiles(id),
                    version TEXT NOT NULL, manifest TEXT NOT NULL,
                    created_at TEXT NOT NULL, created_by TEXT NOT NULL,
                    published_at TEXT,
                    PRIMARY KEY (profile_id, version)
                );
            """)
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _profile(db, profile_id):
        row = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise LauncherStoreError(404, "Unknown launcher profile")
        return row

    @staticmethod
    def _view(row):
        return {"id": row["id"], **json.loads(row["settings"]),
                "revision": row["revision"], "active_version": row["active_version"]}

    def profiles(self):
        with self.connection() as db:
            return [self._view(row) for row in db.execute("SELECT * FROM profiles ORDER BY id")]

    def create_profile(self, payload: NewProfile):
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM profiles WHERE id = ?", (payload.id,)).fetchone():
                raise LauncherStoreError(409, "Profile ID already exists")
            if db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] >= 1000:
                raise LauncherStoreError(409, "The profile limit has been reached")
            db.execute("INSERT INTO profiles (id, settings) VALUES (?, ?)",
                       (payload.id, canonical_json(payload.model_dump(exclude={"id"}))))
            return self._view(self._profile(db, payload.id))

    def update_profile(self, profile_id: str, payload: ProfileUpdate):
        with self.connection(write=True) as db:
            row = self._profile(db, profile_id)
            self._check_revision(row, payload.expected_revision)
            db.execute("UPDATE profiles SET settings = ?, revision = revision + 1 WHERE id = ?",
                       (canonical_json(payload.model_dump(exclude={"expected_revision"})), profile_id))
            return self._view(self._profile(db, profile_id))

    @staticmethod
    def _check_revision(row, expected):
        if row["revision"] != expected:
            raise LauncherStoreError(409, "Profile changed in another session. Reload before saving or publishing.")

    def releases(self, profile_id):
        with self.connection() as db:
            self._profile(db, profile_id)
            return [{"manifest": json.loads(row["manifest"]), "created_at": row["created_at"],
                     "created_by": row["created_by"], "published_at": row["published_at"]}
                    for row in db.execute(
                        "SELECT * FROM releases WHERE profile_id = ? ORDER BY created_at DESC, version",
                        (profile_id,))]

    def create_release(self, profile_id: str, payload: ReleaseSpec, actor: str):
        manifest = ReleaseManifest(profile_id=profile_id, **payload.model_dump())
        with self.connection(write=True) as db:
            self._profile(db, profile_id)
            if db.execute("SELECT 1 FROM releases WHERE profile_id = ? AND version = ?",
                          (profile_id, payload.version)).fetchone():
                raise LauncherStoreError(409, "Release versions are immutable. Use a new version.")
            db.execute("""INSERT INTO releases
                       (profile_id, version, manifest, created_at, created_by) VALUES (?, ?, ?, ?, ?)""",
                       (profile_id, payload.version, canonical_json(manifest.model_dump()),
                        datetime.now(timezone.utc).isoformat(), actor))
        return manifest

    def publish(self, profile_id: str, version: str | None, expected_revision: int):
        with self.connection(write=True) as db:
            row = self._profile(db, profile_id)
            self._check_revision(row, expected_revision)
            if version is not None:
                if not db.execute("SELECT 1 FROM releases WHERE profile_id = ? AND version = ?",
                                  (profile_id, version)).fetchone():
                    raise LauncherStoreError(404, "Unknown release for this profile")
                db.execute("""UPDATE releases SET published_at = COALESCE(published_at, ?)
                              WHERE profile_id = ? AND version = ?""",
                           (datetime.now(timezone.utc).isoformat(), profile_id, version))
            db.execute("UPDATE profiles SET active_version = ?, revision = revision + 1 WHERE id = ?",
                       (version, profile_id))
            return self._view(self._profile(db, profile_id))

    def catalog(self) -> Catalog:
        with self.connection() as db:
            rows = db.execute("""SELECT p.id, p.settings, r.manifest FROM profiles p JOIN releases r
                ON p.id = r.profile_id AND p.active_version = r.version
                WHERE r.published_at IS NOT NULL ORDER BY p.id""")
            profiles = []
            for row in rows:
                settings = json.loads(row["settings"])
                profiles.append(CatalogProfile(id=row["id"], name=settings["name"],
                    description=settings["description"], release=ReleaseManifest.model_validate_json(row["manifest"])))
            return Catalog(profiles=profiles)

    def public_release(self, profile_id, version):
        with self.connection() as db:
            row = db.execute("""SELECT r.manifest FROM releases r JOIN profiles p ON p.id = r.profile_id
                WHERE r.profile_id = ? AND r.version = ? AND r.published_at IS NOT NULL
                AND p.active_version IS NOT NULL""", (profile_id, version)).fetchone()
            if row is None:
                raise LauncherStoreError(404, "Release is not published")
            return ReleaseManifest.model_validate_json(row["manifest"])
