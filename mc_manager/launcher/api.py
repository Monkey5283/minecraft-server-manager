"""Administrator routes and an opt-in, read-only player catalog."""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .models import NewProfile, ProfileUpdate, Publication, ReleaseSpec
from .store import LauncherStore, LauncherStoreError, canonical_json


def install_launcher_routes(app: FastAPI, config, panel, servers) -> None:
    store = LauncherStore(config.agent_registry_file.with_name("launcher.sqlite3"))
    app.state.launcher = store

    @app.exception_handler(LauncherStoreError)
    async def launcher_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    def administrator(request: Request):
        panel.require(request, admin=True)

    def known_server(server_id):
        if server_id is not None and server_id not in servers:
            raise HTTPException(404, "Unknown associated server")

    admin = APIRouter(prefix="/api/launcher", dependencies=[Depends(administrator)])

    @admin.get("")
    def overview():
        return {"profiles": store.profiles(),
                "servers": [{"id": server.id, "name": server.name} for server in servers.values()],
                "public_catalog_enabled": config.launcher_public_catalog_enabled,
                "download_url": config.launcher_download_url or "https://launcher.monkeyservers.com/",
                "catalog_path": "/launcher/v1/catalog"}

    @admin.post("/profiles", status_code=201)
    def create_profile(payload: NewProfile):
        known_server(payload.server_id)
        return store.create_profile(payload)

    @admin.put("/profiles/{profile_id}")
    def update_profile(profile_id: str, payload: ProfileUpdate):
        known_server(payload.server_id)
        return store.update_profile(profile_id, payload)

    @admin.get("/profiles/{profile_id}/releases")
    def releases(profile_id: str):
        return store.releases(profile_id)

    @admin.post("/profiles/{profile_id}/releases", status_code=201)
    def create_release(profile_id: str, payload: ReleaseSpec, request: Request):
        return store.create_release(profile_id, payload, request.session["username"])

    @admin.put("/profiles/{profile_id}/publication")
    def publish(profile_id: str, payload: Publication):
        return store.publish(profile_id, payload.version, payload.expected_revision)

    @admin.get("/catalog")
    def export_catalog():
        return Response(canonical_json(store.catalog().model_dump()) + "\n", media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="launcher-catalog.json"',
                                 "Cache-Control": "no-store"})

    def public_enabled():
        if not config.launcher_public_catalog_enabled:
            raise HTTPException(404, "Player catalog is disabled")

    public = APIRouter(prefix="/launcher/v1", dependencies=[Depends(public_enabled)])

    @public.get("/catalog")
    def catalog():
        return JSONResponse(store.catalog().model_dump(), headers={"Cache-Control": "no-store"})

    @public.get("/profiles/{profile_id}/releases/{version}")
    def manifest(profile_id: str, version: str):
        return JSONResponse(store.public_release(profile_id, version).model_dump(),
                            headers={"Cache-Control": "no-store"})

    app.include_router(admin)
    app.include_router(public)
