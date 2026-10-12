"""Controller-owned restart scripts and persistent recovery monitoring."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .client import action_origin
from .panel import RestartSchedule
from .ups import has_confirmed_line_power, requires_protective_shutdown

LOG = logging.getLogger(__name__)


class RecoverySettings(BaseModel):
    enabled: bool = False
    check_interval_seconds: int = Field(default=30, ge=10, le=3600)
    failure_threshold: int = Field(default=2, ge=1, le=10)
    cooldown_seconds: int = Field(default=300, ge=60, le=3600)
    max_restarts_per_hour: int = Field(default=3, ge=1, le=12)
    recover_after_power_loss: bool = True
    discord_notify: bool = True


def due_restart(raw, now):
    schedule = RestartSchedule.model_validate(raw)
    if not schedule.enabled:
        return None
    zone = ZoneInfo(schedule.timezone)
    local = now.astimezone(zone)
    # Look ahead across midnight for warning periods, and skip nonexistent DST times.
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in schedule.days:
            continue
        for clock in schedule.times:
            hour, minute = map(int, clock.split(":"))
            target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            utc = target.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) != target.replace(tzinfo=None):
                continue
            slot = target.strftime("%Y-%m-%d %H:%M")
            seconds = (utc - now).total_seconds()
            if schedule.warning_seconds and seconds < min(10, schedule.warning_seconds):
                # Do not surprise players with an immediate restart after missing the warning window.
                continue
            if -60 < seconds <= schedule.warning_seconds and slot != raw.get("last_slot"):
                return slot, utc.timestamp()
    return None


class ServerAutomation:
    def __init__(self, store, agents, servers, announce, *, ups_enabled=False, clock=time.time, sleep=asyncio.sleep):
        self.store, self.agents, self.servers, self.announce = store, agents, servers, announce
        self.clock, self.sleep = clock, sleep
        self.ups_enabled = ups_enabled
        self.line_since = None
        self.power_confirmed = not ups_enabled
        self.tasks = {}
        self.checks = {}
        self.next_check = {}

    def state(self, server_id):
        return self.store.data["runtime"].get(server_id, {})

    def record(self, server_id, **values):
        current = self.state(server_id)
        updated = {**current, **values}
        if updated != current:
            self.store.put("runtime", server_id, updated)

    def settings(self, server_id):
        return RecoverySettings.model_validate(self.store.data["monitoring"].get(server_id, {}))

    def blocked(self):
        return not self.power_confirmed or self.store.data["power"].get("outage", {}).get("active", False)

    def power_reading(self, reading):
        if not reading.available or not has_confirmed_line_power(reading.status):
            self.line_since = None
            self.power_confirmed = False
            if reading.available and requires_protective_shutdown(reading.status):
                if not self.store.data["power"].get("outage", {}).get("active"):
                    self.store.put("power", "outage", {"active": True, "recorded_at": self.clock()})
            return
        if self.line_since is None:
            self.line_since = self.clock()
        if self.clock() - self.line_since >= 60:
            self.power_confirmed = True
            if self.store.data["power"].get("outage", {}).get("active"):
                self.store.put("power", "outage", {"active": False, "restored_at": self.clock()})

    def observe_operation(self, server, method, path, payload, origin):
        if origin in {"scheduled", "recovery"}:
            return
        if origin == "power":
            previous = self.state(server.id)
            self.record(server.id, power_pending=previous.get("desired_running") is True,
                        last_result="Stopped for a logged power outage")
            return
        operation = path.rsplit("/", 1)[-1]
        if method == "POST" and operation == "console":
            operation = (payload or {}).get("command", "").strip().lstrip("/").lower().split(" ")[0].rsplit(":", 1)[-1]
            if operation not in {"stop", "end", "shutdown", "restart"}:
                return
        elif method not in {"POST", "PUT", "DELETE"}:
            return
        stopping = operation in {"stop", "end", "shutdown"} or method == "DELETE"
        starting = operation in {"start", "restart"}
        if "/scripts/" in path and "shutdown" in operation:
            for other in tuple(self.servers.values()):
                if other.agent_url == server.agent_url:
                    self.observe_operation(other, "POST", "/actions/stop", None, "admin")
            return
        if stopping:
            task = self.tasks.get(server.id)
            if task:
                task.cancel()
            self.record(server.id, desired_running=False, intentional=True, intent_reason="admin_stop", power_pending=False,
                        pending=None, last_result="Intentionally stopped by an administrator")
        elif starting:
            task = self.tasks.get(server.id)
            if task:
                task.cancel()
            self.record(server.id, desired_running=True, intentional=False, intent_reason=None, power_pending=False,
                        maintenance_until=self.clock() + 600, offline_checks=0, pending=None,
                        last_result="Administrator requested " + operation)
        else:
            # Opaque maintenance scripts may deliberately stop the service.
            self.record(server.id, desired_running=False, intentional=True, intent_reason="maintenance",
                        maintenance_until=self.clock() + 3600,
                        last_result="Administrator maintenance; recovery paused until server returns online")

    async def wait_job(self, server, job):
        deadline = self.clock() + 600
        while self.clock() < deadline:
            result = await self.agents.job(server, job["id"])
            if result["state"] == "succeeded":
                return
            if result["state"] == "failed":
                raise RuntimeError(result.get("error") or "Server operation failed")
            await self.sleep(2)
        raise RuntimeError("Server operation timed out")

    def allowed(self, server_id):
        if server_id not in self.servers:
            raise RuntimeError("Server is no longer registered")
        if self.blocked():
            raise RuntimeError("Restart paused until line power has been confirmed for 60 seconds")
        if self.state(server_id).get("intentional"):
            raise RuntimeError("Restart canceled by an intentional administrator stop")

    async def wait_state(self, server, expected):
        deadline = self.clock() + 600
        confirmations = 0
        while self.clock() < deadline:
            status = await self.agents.status(server)
            if status["state"] == expected:
                if expected == "online" and status.get("player_tracking_available"):
                    try:
                        await self.agents.players(server)
                    except Exception:
                        confirmations = 0
                        await self.sleep(5)
                        continue
                confirmations += 1
                if expected == "offline" or confirmations >= 2:
                    return
            else:
                confirmations = 0
            await self.sleep(5)
        raise RuntimeError(f"Server did not become {expected}")

    async def finish(self, server, notify):
        completed = int(self.clock())
        self.record(server.id, pending=None, desired_running=True, intentional=False,
                    intent_reason=None, power_pending=False, offline_checks=0, last_seen="online",
                    last_restart=completed, last_result="The Server has been Restarted.",
                    maintenance_until=0)
        raw = self.store.data["schedules"].get(server.id)
        if raw:
            self.store.put("schedules", server.id, {**raw, "last_result": "The Server has been Restarted."})
        if notify:
            name = " ".join(server.name.splitlines())
            self.record(server.id, discord_pending=f"{name}\n<t:{completed}:F>\nThe Server has been Restarted.")
            await self.send_notification(server.id)

    async def send_notification(self, server_id):
        state = self.state(server_id)
        if not state.get("discord_pending") or self.clock() < state.get("discord_retry_at", 0):
            return
        self.record(server_id, discord_retry_at=self.clock() + 60)
        try:
            delivered = await self.announce(state["discord_pending"])
            if delivered is not False:
                self.record(server_id, discord_pending=None)
        except Exception:
            LOG.exception("Restart Discord notification pending for %s", server_id)

    async def restart(self, server_id, target, schedule, *, recovery=False):
        try:
            self.allowed(server_id)
            server = self.servers[server_id]
            status = await self.agents.status(server)
            if recovery and status["state"] != "offline":
                self.record(server_id, pending=None, last_result="Recovery skipped: server is no longer offline")
                return
            if not recovery and status["state"] != "online":
                raise RuntimeError("Scheduled restart skipped: server is offline or busy")
            self.allowed(server_id)
            self.record(server_id, desired_running=True, pending={"kind": "recovery" if recovery else "scheduled", "stage": "warning", "notify": schedule.discord_notify},
                        last_result="Preparing restart")
            if not recovery:
                targets = schedule.warning_servers or [server_id]
                if any("velocity" in item.lower() or "proxy" in item.lower() for item in targets):
                    raise RuntimeError("Choose gameplay servers as warning destinations for a proxy restart")
                warning_targets = [self.servers[item] for item in targets]
                remaining = max(0, int(target - self.clock()))
                for warn in warning_targets:
                    warn_status = await self.agents.status(warn)
                    if warn_status["state"] != "online" or not warn_status.get("console_enabled"):
                        raise RuntimeError(f"Cannot deliver player warnings on {warn.name}; restart canceled")
                    await self.agents.console_command(warn, f"say [Server restart] {server.name} will restart in {remaining} seconds. Please prepare to disconnect.")
                for threshold in (60, 10, 0):
                    delay = target - self.clock() - threshold
                    if delay <= 0 and threshold != 0:
                        continue
                    if delay > 0:
                        await self.sleep(delay)
                    self.allowed(server_id)
                    for warn in warning_targets:
                        await self.agents.console_command(warn, f"say [Server restart] {server.name} restarting {'now' if threshold == 0 else 'in ' + str(threshold) + ' seconds'}.")
                self.allowed(server_id)
                self.record(server_id, pending={"kind": "scheduled", "stage": "stopping", "notify": schedule.discord_notify}, last_result="Gracefully stopping server")
                with action_origin("scheduled"):
                    if status.get("console_enabled") and "velocity" not in server.id.lower() and "proxy" not in server.id.lower():
                        await self.agents.console_command(server, "save-all flush")
                    # Use the host's allowlisted service stop: JVM shutdown hooks save worlds.
                    job = await self.agents.action(server, "stop")
                    self.record(server_id, pending={**self.state(server_id)["pending"], "job_id": job["id"]})
                    await self.wait_job(server, job)
                    await self.wait_state(server, "offline")
            self.allowed(server_id)
            self.record(server_id, pending={"kind": "recovery" if recovery else "scheduled", "stage": "starting", "notify": schedule.discord_notify}, last_result="Starting server")
            with action_origin("recovery" if recovery else "scheduled"):
                job = await self.agents.action(server, "start")
                self.record(server_id, pending={**self.state(server_id)["pending"], "job_id": job["id"]})
                await self.wait_job(server, job)
                await self.wait_state(server, "online")
            await self.finish(server, schedule.discord_notify)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("Server automation failed for %s", server_id)
            self.record(server_id, pending=None, last_result=str(exc))
            raw = self.store.data["schedules"].get(server_id)
            if raw and not recovery:
                self.store.put("schedules", server_id, {**raw, "last_result": str(exc)})

    def launch(self, server_id, target, schedule, *, recovery=False):
        if server_id in self.tasks and not self.tasks[server_id].done():
            return False
        self.tasks[server_id] = asyncio.create_task(self.restart(server_id, target, schedule, recovery=recovery))
        return True

    async def check_server(self, server_id):
        settings = self.settings(server_id)
        if not settings.enabled:
            return
        server = self.servers[server_id]
        try:
            status = await self.agents.status(server)
        except Exception:
            # Network failures cannot establish that a Minecraft process has stopped.
            self.record(server_id, last_result="Agent unreachable; waiting for host", offline_checks=0)
            return
        now = self.clock()
        # Administrator intent may have changed while the agent status request was in flight.
        state = self.state(server_id)
        if server_id in self.tasks and not self.tasks[server_id].done():
            return
        if status["state"] == "online":
            returning = state.get("intent_reason") == "maintenance" and (
                state.get("last_seen") in {"offline", "busy"} or now >= state.get("maintenance_until", 0)
            )
            self.record(server_id, last_seen="online", offline_checks=0,
                        desired_running=True if returning or state.get("desired_running") is None else state.get("desired_running"),
                        intentional=False if returning else state.get("intentional", False))
            if state.get("power_pending") and settings.recover_after_power_loss and not self.blocked():
                await self.finish(server, settings.discord_notify)
            return
        if status["state"] != "offline":
            self.record(server_id, last_seen=status["state"], offline_checks=0)
            return
        count = min(settings.failure_threshold, state.get("offline_checks", 0) + 1)
        self.record(server_id, last_seen="offline", offline_checks=count)
        if state.get("intentional") or state.get("desired_running") is not True or self.blocked():
            return
        if state.get("power_pending") and not settings.recover_after_power_loss:
            return
        if count < settings.failure_threshold or now < state.get("maintenance_until", 0):
            return
        attempts = [item for item in state.get("attempts", []) if now - item < 3600]
        if len(attempts) >= settings.max_restarts_per_hour:
            self.record(server_id, last_result="Hourly recovery limit reached; inspect the server")
            return
        if attempts and now - attempts[-1] < settings.cooldown_seconds:
            return
        self.record(server_id, attempts=attempts + [now], last_result="Recovering unexpected shutdown")
        self.launch(server_id, now, RestartSchedule(warning_seconds=0, discord_notify=settings.discord_notify), recovery=True)

    async def tick(self):
        now = self.clock()
        for server_id in tuple(self.servers):
            if server_id in self.tasks and not self.tasks[server_id].done():
                continue
            raw = self.store.data["schedules"].get(server_id)
            due = due_restart(raw, datetime.fromtimestamp(now, timezone.utc)) if raw else None
            if due and not self.blocked() and not self.state(server_id).get("intentional"):
                slot, target = due
                self.store.put("schedules", server_id, {**raw, "last_slot": slot, "last_result": "Restart script running"})
                self.launch(server_id, target, RestartSchedule.model_validate(raw))
                continue
            if now >= self.next_check.get(server_id, 0):
                check = self.checks.get(server_id)
                if check is None or check.done():
                    self.next_check[server_id] = now + self.settings(server_id).check_interval_seconds
                    self.checks[server_id] = asyncio.create_task(self.poll_server(server_id))

    async def poll_server(self, server_id):
        try:
            await self.send_notification(server_id)
            await self.check_server(server_id)
        except Exception:
            LOG.exception("Monitor check failed for %s", server_id)

    async def resume(self, server_id, pending):
        try:
            if pending["stage"] == "warning":
                self.record(server_id, pending=None, last_result="Countdown interrupted by controller restart; missed run skipped")
                return
            server = self.servers[server_id]
            while self.blocked():
                await self.sleep(5)
            self.allowed(server_id)
            if pending.get("job_id"):
                try:
                    await self.wait_job(server, {"id": pending["job_id"]})
                except Exception:
                    # The agent may also have rebooted and lost its in-memory job list.
                    LOG.warning("Reconciling interrupted job for %s from live server state", server_id)
            status = await self.agents.status(server)
            if status["state"] == "offline":
                await self.restart(server_id, self.clock(), RestartSchedule(warning_seconds=0, discord_notify=pending["notify"]), recovery=True)
            elif status["state"] == "online" and pending["stage"] == "starting":
                await self.wait_state(server, "online")
                await self.finish(server, pending["notify"])
            else:
                self.record(server_id, pending=None, last_result="Interrupted restart reconciled; no duplicate restart issued")
        except Exception as exc:
            self.record(server_id, pending=None, last_result="Interrupted restart needs attention: " + str(exc))

    async def run(self):
        for server_id, state in tuple(self.store.data["runtime"].items()):
            if state.get("pending") and server_id in self.servers:
                self.tasks[server_id] = asyncio.create_task(self.resume(server_id, state["pending"]))
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    LOG.exception("Server automation iteration failed")
                await self.sleep(5)
        finally:
            tasks = [*self.tasks.values(), *self.checks.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
