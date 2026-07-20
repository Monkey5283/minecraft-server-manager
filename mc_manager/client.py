from __future__ import annotations

import httpx

from .config import RemoteServer


class AgentUnavailable(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class AgentClient:
    def __init__(self, timeout: float = 35.0):
        self.client = httpx.AsyncClient(timeout=timeout)

    @staticmethod
    def _headers(server: RemoteServer) -> dict[str, str]:
        return {"Authorization": f"Bearer {server.token}"}

    async def _request(
        self,
        server: RemoteServer,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        content: bytes | None = None,
    ) -> dict | list:
        try:
            response = await self.client.request(
                method,
                f"{server.agent_url}{path}",
                headers=self._headers(server),
                params=params,
                json=json_body,
                content=content,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except ValueError:
                detail = exc.response.text
            raise AgentUnavailable(
                f"{server.name}: agent returned {exc.response.status_code}: {detail}",
                status_code=exc.response.status_code,
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise AgentUnavailable(f"{server.name}: agent is unreachable") from exc

    async def status(self, server: RemoteServer) -> dict:
        entries = await self.statuses(server)
        if server.id in entries:
            return entries[server.id]
        raise AgentUnavailable(f"{server.name}: server is not configured on its agent")

    async def info(self, server: RemoteServer) -> dict:
        result = await self._request(server, "GET", "/v1/info")
        if not isinstance(result, dict) or not isinstance(result.get("servers"), list):
            raise AgentUnavailable(f"{server.name}: agent returned invalid identity data")
        return result

    async def catalog(self, server: RemoteServer, server_type: str) -> dict:
        result = await self._request(server, "GET", f"/v1/catalog/{server_type}")
        if not isinstance(result, dict) or not isinstance(result.get("versions"), list):
            raise AgentUnavailable(f"{server.name}: agent returned an invalid version list")
        return result

    async def provision(self, server: RemoteServer, payload: dict) -> dict:
        result = await self._request(
            server,
            "POST",
            "/v1/provision",
            json_body=payload,
        )
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise AgentUnavailable(f"{server.name}: agent returned an invalid install job")
        return result

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

    async def files(self, server: RemoteServer, path: str = "") -> dict:
        result = await self._request(
            server,
            "GET",
            f"/v1/servers/{server.id}/files",
            params={"path": path},
        )
        if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
            raise AgentUnavailable(f"{server.name}: agent returned an invalid file list")
        return result

    async def file_content(self, server: RemoteServer, path: str) -> dict:
        result = await self._request(
            server,
            "GET",
            f"/v1/servers/{server.id}/files/content",
            params={"path": path},
        )
        if not isinstance(result, dict) or not isinstance(result.get("content"), str):
            raise AgentUnavailable(f"{server.name}: agent returned invalid file content")
        return result

    async def download_file(
        self,
        server: RemoteServer,
        path: str,
    ) -> httpx.Response:
        request = self.client.build_request(
            "GET",
            f"{server.agent_url}/v1/servers/{server.id}/files/download",
            headers=self._headers(server),
            params={"path": path},
        )
        try:
            response = await self.client.send(request, stream=True)
            if response.is_error:
                await response.aread()
                response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except ValueError:
                detail = exc.response.text
            await exc.response.aclose()
            raise AgentUnavailable(
                f"{server.name}: agent returned {exc.response.status_code}: {detail}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise AgentUnavailable(f"{server.name}: agent is unreachable") from exc

    async def save_file(
        self,
        server: RemoteServer,
        path: str,
        content: str,
        expected_version: str | None,
    ) -> dict:
        result = await self._request(
            server,
            "PUT",
            f"/v1/servers/{server.id}/files/content",
            json_body={
                "path": path,
                "content": content,
                "expected_version": expected_version,
            },
        )
        assert isinstance(result, dict)
        return result

    async def create_directory(self, server: RemoteServer, path: str) -> dict:
        result = await self._request(
            server,
            "POST",
            f"/v1/servers/{server.id}/files/directory",
            json_body={"path": path},
        )
        assert isinstance(result, dict)
        return result

    async def delete_file(self, server: RemoteServer, path: str) -> dict:
        result = await self._request(
            server,
            "DELETE",
            f"/v1/servers/{server.id}/files",
            params={"path": path},
        )
        assert isinstance(result, dict)
        return result

    async def console_output(self, server: RemoteServer, cursor: int = 0) -> dict:
        result = await self._request(
            server,
            "GET",
            f"/v1/servers/{server.id}/console",
            params={"cursor": cursor},
        )
        if not isinstance(result, dict) or not isinstance(result.get("content"), str):
            raise AgentUnavailable(f"{server.name}: agent returned invalid console output")
        return result

    async def console_command(self, server: RemoteServer, command: str) -> dict:
        result = await self._request(
            server,
            "POST",
            f"/v1/servers/{server.id}/console",
            json_body={"command": command},
        )
        assert isinstance(result, dict)
        return result

    async def upload_file(
        self,
        server: RemoteServer,
        path: str,
        content: bytes,
        *,
        overwrite: bool,
    ) -> dict:
        result = await self._request(
            server,
            "PUT",
            f"/v1/servers/{server.id}/files/upload",
            params={"path": path, "overwrite": str(overwrite).lower()},
            content=content,
        )
        assert isinstance(result, dict)
        return result

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
