from __future__ import annotations

import httpx

from .config import RemoteServer


class AgentUnavailable(RuntimeError):
    pass


class AgentClient:
    def __init__(self, timeout: float = 35.0):
        self.client = httpx.AsyncClient(timeout=timeout)

    @staticmethod
    def _headers(server: RemoteServer) -> dict[str, str]:
        return {"Authorization": f"Bearer {server.token}"}

    async def _request(
        self, server: RemoteServer, method: str, path: str
    ) -> dict | list:
        try:
            response = await self.client.request(
                method,
                f"{server.agent_url}{path}",
                headers=self._headers(server),
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except ValueError:
                detail = exc.response.text
            raise AgentUnavailable(
                f"{server.name}: agent returned {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise AgentUnavailable(f"{server.name}: agent is unreachable") from exc

    async def status(self, server: RemoteServer) -> dict:
        entries = await self.statuses(server)
        if server.id in entries:
            return entries[server.id]
        raise AgentUnavailable(f"{server.name}: server is not configured on its agent")

    async def statuses(self, server: RemoteServer) -> dict[str, dict]:
        entries = await self._request(server, "GET", "/v1/servers")
        if not isinstance(entries, list):
            raise AgentUnavailable(f"{server.name}: agent returned an invalid status list")
        results: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise AgentUnavailable(
                    f"{server.name}: agent returned an invalid server status"
                )
            results[entry["id"]] = entry
        return results

    async def players(self, server: RemoteServer) -> tuple[str, ...]:
        result = await self._request(
            server,
            "GET",
            f"/v1/servers/{server.id}/players",
        )
        if not isinstance(result, dict) or not isinstance(result.get("players"), list):
            raise AgentUnavailable(
                f"{server.name}: agent returned an invalid player snapshot"
            )
        players = result["players"]
        if not all(isinstance(player, str) and player for player in players):
            raise AgentUnavailable(
                f"{server.name}: agent returned an invalid player name"
            )
        return tuple(players)

    async def action(self, server: RemoteServer, action: str) -> dict:
        result = await self._request(
            server, "POST", f"/v1/servers/{server.id}/actions/{action}"
        )
        assert isinstance(result, dict)
        return result

    async def script(self, server: RemoteServer, script_name: str) -> dict:
        result = await self._request(
            server, "POST", f"/v1/servers/{server.id}/scripts/{script_name}"
        )
        assert isinstance(result, dict)
        return result

    async def job(self, server: RemoteServer, job_id: str) -> dict:
        result = await self._request(server, "GET", f"/v1/jobs/{job_id}")
        assert isinstance(result, dict)
        return result

    async def close(self) -> None:
        await self.client.aclose()
