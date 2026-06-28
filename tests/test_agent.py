import sys
import asyncio
from pathlib import Path

import httpx

from mc_manager.agent import create_agent_app
from mc_manager.config import AgentConfig, AgentServer


async def test_agent_requires_token_and_runs_only_configured_action(tmp_path: Path):
    ok_command = ((sys.executable, "-c", "print('started safely')"),)
    server = AgentServer(
        id="survival",
        name="Survival",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={"backup": ok_command},
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        assert (await client.get("/v1/servers")).status_code == 401
        status = await client.get("/v1/servers", headers=headers)
        assert status.status_code == 200
        assert status.json()[0]["state"] == "online"

        forbidden = await client.post(
            "/v1/servers/survival/actions/destroy", headers=headers
        )
        assert forbidden.status_code == 404

        created = await client.post(
            "/v1/servers/survival/actions/start", headers=headers
        )
        assert created.status_code == 200
        job_id = created.json()["id"]

        for _ in range(50):
            job = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
            if job["state"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.02)

        assert job["state"] == "succeeded"
        assert "started safely" in job["output"]
