"""Persistent dashboard accounts and per-server restart schedules."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

LOG = logging.getLogger(__name__)


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"{salt}:{digest}"


def password_matches(password: str, encoded: str) -> bool:
    return hmac.compare_digest(password_hash(password, encoded.split(":")[0]), encoded)


class NewUser(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    password: str = Field(min_length=12, max_length=256)
    admin: bool = False


class PasswordChange(BaseModel):
    password: str = Field(min_length=12, max_length=256)
    current_password: str = Field(default="", max_length=256)


class RestartSchedule(BaseModel):
    enabled: bool = False
    timezone: str = "UTC"
    days: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    times: list[str] = Field(default_factory=lambda: ["04:00"], min_length=1, max_length=24)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone such as America/Edmonton")
        return value

    @field_validator("days")
    @classmethod
    def valid_days(cls, value):
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("Days must be between 0 (Monday) and 6 (Sunday)")
        return sorted(set(value))

    @field_validator("times")
    @classmethod
    def valid_times(cls, value):
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", item) for item in value):
            raise ValueError("Times must use 24-hour HH:MM format")
        return sorted(set(value))


class PanelStore:
    def __init__(self, path: Path, username: str, password: str):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.data.get("users"), dict) or not self.data["users"]:
                raise ValueError("Invalid dashboard account store")
            for schedule in self.data["schedules"].values():
                RestartSchedule.model_validate(schedule)
        except FileNotFoundError:
            self.data = {"users": {username: self.account(password, True)}, "schedules": {}}

    @staticmethod
    def account(password, admin):
        return {"hash": password_hash(password), "admin": admin, "version": secrets.token_hex(16)}

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        self.data = data

    def put(self, section, key, value):
        data = {**self.data, section: {**self.data[section], key: value}}
        self.save(data)

    def user(self, session):
        user = self.data["users"].get(session.get("username"))
        if user and session.get("version") == user["version"]:
            return user
        return None

    def require(self, request, admin=False):
        user = self.user(request.session)
        if not user:
            raise HTTPException(401, "Sign in required")
        if admin and not user["admin"]:
            raise HTTPException(403, "Administrator access required")
        return user

    async def tick(self, agents, servers, now=None):
        now = now or datetime.now(timezone.utc)
        for server_id, raw in list(self.data["schedules"].items()):
            if not raw["enabled"] or server_id not in servers:
                continue
            local = now.astimezone(ZoneInfo(raw["timezone"]))
            slot = local.strftime("%Y-%m-%d %H:%M")
            # Fold=1 is the repeated DST hour; never restart twice for it.
            if (local.fold or local.weekday() not in raw["days"]
                    or local.strftime("%H:%M") not in raw["times"]
                    or raw.get("last_slot") == slot):
                continue
            claimed = {**raw, "last_slot": slot, "last_result": "Checking server"}
            self.put("schedules", server_id, claimed)  # Persist before dispatch: no duplicate retries.
            try:
                state = await agents.status(servers[server_id])
                # A schedule may have been edited while the status request was in flight.
                if self.data["schedules"].get(server_id) != claimed:
                    continue
                if state.get("state") != "online" or state.get("active_job"):
                    result = "Skipped: server offline or busy"
                else:
                    job = await agents.action(servers[server_id], "restart")
                    result = f"Restart submitted (job {job.get('id', 'unknown')})"
            except Exception as exc:
                LOG.exception("Scheduled restart failed for %s", server_id)
                result = f"Failed: {exc}"
            if self.data["schedules"].get(server_id) == claimed:
                self.put("schedules", server_id, {**claimed, "last_result": result})

    async def run(self, agents, servers):
        while True:
            try:
                await self.tick(agents, servers)
            except Exception:
                LOG.exception("Restart scheduler iteration failed")
            await asyncio.sleep(10)
