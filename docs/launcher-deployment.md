# Launcher deployment and security review

Reviewed 2026-10-31. This is a preview release, not a security certification.

## Separate public downloads from private administration

The public download site is https://monkeycraft-launcher.giddymixer.chatgpt.site/.
The requested custom hostname is `launcher.monkeyservers.com`, pending DNS and TLS
validation. The site is static: artwork, download parts, checksums and a player
catalog. It has no controller backend, private-network tunnel or panel credentials.
Keep the manager on its existing Tailscale address with its application login.
Do not point public DNS, Tailscale Funnel or a public reverse proxy at the manager.

Administrators use **Launcher & modpacks** to create profiles, save immutable
releases, select the published version, roll back, withdraw and export a catalog.
The associated panel server is administrative metadata; exported player catalogs
contain only the allowlisted player fields. Keep `[launcher].public_catalog_enabled`
false. The optional `[launcher].download_url` selects the public website link.

Publishing a release in the panel updates its private catalog database. To deliver
it to players, export the published catalog and deploy it as `catalog.json` on the
public website. Changes are intentionally not a tunnel into the controller. Only
publish files meant for clients, with HTTPS URLs, exact sizes and SHA-256 hashes.
Publication does not verify remote files, mod compatibility or redistribution rights.

## Windows package

`tools/build-launcher.ps1` builds the launcher in a separate environment. It bundles
only the client modules and selected artwork, a public Microsoft application ID,
and the public catalog URL. Controller/agent code, local account files and private
configuration are excluded. `tools/prepare-launcher-download.py` splits the
executable into content-addressed static assets small enough for the hosting
provider; the browser verifies each part and the final SHA-256 before saving it.

The preview executable is unsigned. Same-origin checksums detect corrupt downloads
but are not an independent publisher signature. Minecraft API application approval
is still pending: Microsoft sign-in and authenticated Play cannot yet be validated
end to end for public users. No authentication bypass is provided.

## Checks and results

- Manager APIs require a valid panel session centrally; launcher administration
  additionally requires an administrator. Forged/anonymous sessions are rejected.
- Mutations enforce same-origin requests, Fetch Metadata and a custom-header
  fallback for non-browser clients. Cross-origin preflight is not enabled.
- Secure sessions remain HttpOnly, SameSite=Strict and host-only. Controller
  responses disable caching/framing, restrict content sources and use HSTS when
  secure cookies are enabled. The OpenAPI endpoint is disabled.
- Login attempts are rate-limited before expensive password hashing. These bounds
  are per process; keep the controller at one worker or add a shared limiter.
- Full integrated suite: **389 passed, 1 skipped, 3 pre-existing failures**. The
  unchanged main commit `b2126b0` reproduces the three failures: one stale file
  download JavaScript assertion and two portal tests referencing an absent
  `PortalCommand.java`. Tests ran with capture disabled because an existing global
  `os.fstat` mock interferes with pytest 9 output capture.
- Both audited local environments (manager and isolated Windows build) report
  **no known vulnerable dependencies**, and dependency consistency checks pass.
  Updated minimum versions are recorded in `pyproject.toml`. The live Linux
  environment was not independently inventoried by these local audits.
- Bandit reports **0 medium/high findings** and 9 low-severity subprocess/platform
  warnings. Commands use argument lists without a shell; user-controlled commands
  are not accepted by the launcher. This is not a full penetration test.
- Package inspection found no controller, agent, secrets or account files. The
  frozen worker and dependency/artwork self-check passed. The public catalog was
  loaded using the actual launcher client; the production browser download passed
  complete SHA-256 verification.
- Anonymous requests to public `/api/servers`, `/api/launcher`, `/.env`,
  `/config/controller.toml`, `/openapi.json` and `/panel-state.json` returned 404.
  The hosting provider does not apply the supplied `_headers` file: the public
  page enforces its supported CSP directives through a meta tag; controller
  response headers are enforced by the application itself.

Keep signing, application approval, actual account/game play tests and ongoing
dependency updates on the release checklist. Do not present this preview as fully
verified production software.
