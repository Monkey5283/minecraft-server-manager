# Launcher framework

The control panel now manages Minecraft Java client profiles and immutable pack
releases. A native desktop launcher and CLI consume that shared contract and can
install, verify, update, and recover managed pack files, install Minecraft and Java,
authenticate Microsoft accounts, and launch Java Edition clients.

## Available now

- **Launcher & modpacks** in the existing dashboard, visible to administrators.
- Profile names, descriptions, and optional links to registered panel servers.
- Releases with pinned Minecraft/loader versions, Java requirements, memory,
  player server addresses, release notes, and checksummed client files.
- Explicit publication, rollback to any saved release, and withdrawal.
- A portable catalog export and an opt-in read-only player API.
- A desktop window with catalog loading, profile selection, per-profile instances,
  saved library/memory settings, progress, cancellation, and an operation log.
- A Minecraft-inspired desktop theme with bundled scenic artwork, pack thumbnails,
  stone accents, and separate Play, Launcher Setup, and Activity tabs.
- A client library/CLI that validates catalogs and checks installed managed files
  by size and SHA-256, reporting files to download, replace, keep, or remove.
- An installer with HTTPS downloads, staging, checksums, local-change review,
  retained backups, an installation lock, and interrupted-update recovery.
- Official Minecraft libraries/assets/native files and Java runtime installation,
  plus exact-version Fabric, Forge, and NeoForge adapters through the pinned
  BSD-licensed `minecraft-launcher-lib==8.0` dependency.
- Microsoft browser sign-in with PKCE/state verification, token renewal, Xbox
  authorization, Java Edition entitlement/profile checks, and Windows DPAPI storage.
- Authenticated Play, memory/Java selection, instance isolation, process status,
  stop controls, and a bounded output log with access tokens redacted.
- Library status for installed clients, published release changes, missing
  runtimes, and interrupted updates; instance-folder and game-log actions.
- Saved-account retry after Minecraft API approval, without another browser
  sign-in unless Microsoft requires it.

Microsoft sign-in requires your own application registration and any required
Minecraft API approval. These are external setup steps; no borrowed application
ID or offline-account bypass is bundled. The launcher does not resolve mod
dependencies or update its own executable. Publishing validates
the manifest structure; it does not fetch URLs, verify remote bytes, check mod
compatibility, or certify that a release has been play-tested.

## Desktop artwork

The **Play & Modpacks** tab keeps profile artwork, installation status, and game
actions together. **Launcher Setup** contains the catalog, library, memory, and
account/Java settings; **Activity** contains operation output. Account actions and
progress remain visible across all three tabs.

Images are bundled under `mc_manager/launcher/assets`, so the theme does not need
CurseForge installed or download artwork at runtime. The Civilizations title scene,
vanilla landscape, and stone texture were copied unchanged from the existing
Monkeycraft Civilizations instance. `overworld.png` is an original generated voxel
panorama. Private development provenance records the original locations and generation prompt; it is excluded from distribution.
No source instance files were changed.

`artwork.py` selects images by profile name and loader: Civilizations profiles use
their title scene, vanilla clients use the vanilla landscape, and Create/World/
Adventure profiles use the neutral panorama. Other modded profiles currently use
the Monkeycraft Civilizations scene. The artwork scales with the window and also
appears as library thumbnails. Install the `launcher` extra for Pillow image
support; missing Pillow or an unavailable image falls back to the flat theme.

The launcher also has an original voxel monkey icon: the generated transparent master is retained privately, `launcher-icon.png` is the 256px PNG,
and `launcher.ico` contains 16, 20, 24, 32, 40, 48, 64, 128, and 256px Windows
variants. The window and its dialogs use this icon. Before creating the window,
Windows receives the `Monkeycraft.Launcher` application identity so it groups
separately from other Python programs.

Create or refresh desktop and Start menu shortcuts from this checkout:

```powershell
.\tools\create-launcher-shortcuts.ps1
# Optional: point the shortcuts at an existing catalog and account/settings folder.
.\tools\create-launcher-shortcuts.ps1 -Catalog 'D:\path\catalog.json' -DataDirectory 'D:\path\launcher-data'
```

Shortcuts target this checkout's `.venv\Scripts\pythonw.exe`, carry the same
application identity and custom icon, and depend on the checkout staying in place.
Use **Pin to taskbar** on the shortcut if desired. This script does not change
existing pins. Windows identity follows Microsoft's
[AppUserModelID guidance](https://learn.microsoft.com/en-us/windows/win32/shell/appids).

## Control panel workflow

1. Run the updated controller and sign in as an administrator.
2. Open **Launcher & modpacks**, choose **New profile**, and save its ID and name.
3. Optionally associate an existing panel server. This is an administrative
   association only. Enter the player-facing hostname/port separately in each
   release; internal agent URLs and tokens never enter the player catalog.
4. Create a release. Choose Vanilla, Fabric, Forge, or NeoForge. Modded loaders
   require their exact version. Set the Minecraft version and Java major version
   deliberately. Installation checks loader availability for that Minecraft
   version and the selected Java major. Forge versions use just the loader
   version, for example `52.0.28`, without a `1.21.1-` prefix.
5. Supply only client files as a JSON array, or `[]` for vanilla. Each entry has
   `path`, HTTPS `url`, lowercase `sha256`, and byte `size`. Use original download
   sources or files you are permitted to host. No server-directory copying occurs.
6. Select the saved release and click **Publish selected release**. Until this
   step the new release remains outside the player catalog. Selecting an older
   release and publishing it rolls back the catalog's current version.
7. Use **Export published catalog** to obtain `launcher-catalog.json`.

Saved release versions cannot be overwritten or deleted through these APIs.
**Use as template** copies a release into the editor and requires a new version.
**Withdraw profile** removes it and its release endpoints from the player feed
without deleting history. Profile names/descriptions remain editable and changes
to those fields appear in the catalog immediately if the profile is published.
Concurrent edits/publications use a revision check; reload after a conflict.

## Hosting and persistence

No configuration changes are needed to use administrator editing and export.
For a launcher on the same trusted LAN/VPN, enable the player API explicitly:

```toml
[launcher]
public_catalog_enabled = true
```

Restart the controller after changing this setting. The default is `false`, which
makes `/launcher/v1/*` return 404. When enabled, those routes can be read without
panel credentials by anyone with network access to the controller. Only published
client metadata is returned. The admin API always requires an administrator's
existing panel session, independent of this flag.

Keep the control panel on its existing private network. For external players,
host the exported JSON on a separate HTTPS static host and use that URL in the
launcher. Export again after publishing, renaming, rolling back, or withdrawing;
an exported catalog is a snapshot and does not update automatically. Previously
downloaded/exported metadata cannot be recalled by withdrawing a profile.

State is stored in `launcher.sqlite3` next to `discovery.agent_registry_file`
(normally `/var/lib/minecraft-manager/launcher.sqlite3`). The database is created
on first use. SQLite transactions make publication atomic and serialize writers;
release records retain their creator, creation time, and first publication time.
Back it up using SQLite's backup API or while the controller is stopped. Do not
copy a live database file without accounting for its journal. No new service,
network port, or pip dependency is required. The desktop app needs a Python
distribution with Tk support (available in this workspace's Windows environment).

## API contract, version 1

| Method and path | Access | Purpose |
| --- | --- | --- |
| `GET /api/launcher` | Admin | Profiles, revisions, server choices, feed settings |
| `POST /api/launcher/profiles` | Admin | Create `{id, name, description?, server_id?}` |
| `PUT /api/launcher/profiles/{id}` | Admin | Replace metadata with `expected_revision` |
| `GET /api/launcher/profiles/{id}/releases` | Admin | Saved manifests and creation/publication metadata |
| `POST /api/launcher/profiles/{id}/releases` | Admin | Save a new immutable release specification |
| `PUT /api/launcher/profiles/{id}/publication` | Admin | `{version, expected_revision}`; `null` withdraws |
| `GET /api/launcher/catalog` | Admin | Download the published catalog, even with feed disabled |
| `GET /launcher/v1/catalog` | Opt-in player API | Published profiles and their complete current manifests |
| `GET /launcher/v1/profiles/{id}/releases/{version}` | Opt-in player API | Previously published manifest of a currently visible profile |

Unknown keys are rejected. IDs are lowercase letters/digits, `_`, or `-`, up to
64 characters. Releases carry `schema_version: 1` and `profile_id`. Catalogs also
carry `schema_version: 1`; their `profiles` entries contain `id`, `name`,
`description`, and the complete `release`. Unsupported schema versions fail
validation in the client rather than being interpreted as version 1.

Example release specification submitted by an administrator:

```json
{
  "version": "1.0.0",
  "minecraft_version": "1.21.1",
  "loader": {"kind": "vanilla", "version": null},
  "java_major": 21,
  "memory_mb": 4096,
  "server_address": "play.example.net:25565",
  "changelog": "Initial vanilla profile.",
  "files": []
}
```

File targets must be inside `mods/`, `config/`, `defaultconfigs/`, `resourcepacks/`,
`shaderpacks/`, or `kubejs/`. Traversal, absolute paths, Windows reserved names,
case-insensitive duplicates, and file/directory collisions are rejected. Vanilla
releases cannot include files in `mods/`. Download URLs require HTTPS without
embedded credentials or fragments. Commands, JVM arguments, and scripts to run
are not part of this contract. KubeJS source is pack content for the game to load.

Limits: 4,096 files per release, 2 GiB per file, 32 GiB total file bytes per release,
and 1,000 profiles. The client caps incoming catalog JSON at 8 MiB and does not
follow redirects. Keep the combined published catalog below that limit; larger
catalogs will need a later paginated/per-profile discovery protocol.

The typed source is `mc_manager/launcher/models.py`. Consumers can generate JSON
Schema with `Catalog.model_json_schema()` or `ReleaseManifest.model_json_schema()`.

## Desktop launcher

From this checkout, start the app with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[launcher]"
.\.venv\Scripts\python.exe -m mc_manager.launcher.desktop
# Or start it without a console window:
.\tools\start-launcher.ps1
# After installing the project with the launcher extra:
mc-manager-launcher-desktop
```

Paste the panel's full `/launcher/v1/catalog` URL or browse to an exported catalog.
Use **Load catalog**, select a profile, and choose an instance library directory.
**Check files** previews pack changes. **Install / repair** installs the pack,
Minecraft, official assets/libraries, Java, and the exact loader. Configure the
application ID under **Account / Java settings**, use **Sign in with Microsoft**,
then **Play**. Play refreshes the saved account, checks Java and the installed
release, and runs the game in its profile directory. The server address is
displayed for adding in Minecraft's multiplayer screen; automatic joining is
not enabled. Close Minecraft and editors using that instance before an
update. If a pack file has local changes, the app lists the conflicts and asks
before replacing/removing them; recovery backups are retained either way.

Preferences live under `%LOCALAPPDATA%\MonkeycraftLauncher\settings.json` on
Windows, or `$XDG_DATA_HOME/monkeycraft-launcher` (normally
`~/.local/share/monkeycraft-launcher`) elsewhere. Settings contain only the public
client ID and preferences. On Windows, a renewable Microsoft token is stored
separately in `microsoft-account.json`, encrypted with current-user DPAPI; game
access tokens remain in memory. **Sign out** removes the stored account. Browser
Microsoft cookies are unaffected. On other platforms tokens are memory-only,
so sign in and play in the same desktop session.
The default library lives beneath that directory. Instances use
`<library>/instances/profile-<profile-id>`. Use separate libraries for unrelated
catalog publishers that reuse the same profile IDs. The memory limit becomes
Java's maximum heap (`-Xmx`). Leave the Java path empty for Mojang's runtime or
select an absolute `java.exe`/`java` path with the profile's exact Java major.

For isolated previews, `--catalog <file-or-url>` and
`--data-directory <absolute-directory>` override the initial catalog and preference
location. Invalid saved settings produce an error rather than being discarded.

The **On this PC** column compares the installed release with the loaded catalog.
**Release changed** covers both updates and administrator rollbacks; **Update**
installs the currently published version after the normal file-change review.
**Repair needed** indicates incomplete Minecraft/Java records or missing paths.
**Recovery needed** enables Recover for interrupted pack transactions. A different
profile in the same folder, unreadable metadata, or a release changed without a new
version is shown as **Needs attention** or a specific conflict.

**Refresh status** rereads local installation records. **Load catalog** also checks
the panel's latest published releases. Status checks do not download files, run
Java, or contact Microsoft. **Installed** describes local records and required
paths, not a full file-integrity check or proof of account ownership. **Check files**
verifies managed pack hashes; Play still performs its own runtime/account checks.
Open folder opens the selected instance; Game log shows the last 256 KiB of the
launcher's captured output. Copy server copies the published multiplayer address.

After restarting, a saved Microsoft account is shown as saved, rather than signed
in, until Minecraft access is verified. **Retry saved account** refreshes that
account without opening a browser. If Minecraft rejects the application's access,
the Microsoft session remains encrypted locally so it can be retried after API
approval. A revoked session requires a fresh browser sign-in. Successful retry
still requires Java Edition ownership and a valid Minecraft profile.

## Microsoft application setup

If Entra opens the **Microsoft Services** directory and reports `AADSTS16000`
or says applications outside a directory are deprecated, that personal account
does not yet have a usable directory for this registration. Use an existing
Azure/Microsoft 365 directory where you can register apps, or follow Microsoft's
[Azure free-account signup](https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account)
to establish one. Complete identity/billing verification and agreements yourself.
After signup, switch Entra to the new directory and return to App registrations.
Creating a cloud VM or deploying the Minecraft server to Azure is not required.

1. Open [Microsoft Entra app registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
   and create an application for this launcher. **Personal accounts only** is
   sufficient for the launcher's consumers endpoint. Review the Microsoft
   Platform Policies linked beside **Register** before completing registration.
2. Select **Public client/native (mobile & desktop)** during registration, or
   add a **Mobile and desktop applications** platform afterward, with the redirect URI
   **`http://localhost`**. This launcher listens on an ephemeral loopback port;
   Microsoft ignores the port when matching a localhost redirect. Do not select
   Web or SPA, and do not create or distribute a client secret. See Microsoft's
   [native redirect guidance](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url#localhost-exceptions).
3. Copy the **Application (client) ID** into **Account / Java settings**. The
   launcher requests `XboxLive.signin offline_access` through the consumers
   authorization endpoint, using the [authorization-code flow with PKCE](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow).
4. Apply for Minecraft API access through the [Minecraft application review form](https://aka.ms/mce-reviewappid)
   if required for this new app. The library's [Microsoft login documentation](https://minecraft-launcher-lib.readthedocs.io/en/stable/tutorial/microsoft_login.html)
   describes this additional approval. A successful Microsoft consent screen
   alone does not establish permission to call Minecraft's API.
   The current form also asks for the Directory (tenant) ID, a contact email
   matching the Azure Portal, a public website/domain describing the application,
   and a justification. Its introductory text says submissions are reviewed
   weekly, so registration alone does not guarantee same-day game sign-in.
   Use **Monkeycraft Launcher** as the display name; the form excludes names
   containing Mojang, Minecraft, Microsoft, Live, Xbox, Discord, or Hypixel.
5. Restart/load the launcher, choose **Sign in with Microsoft**, and finish the
   account picker, password, MFA, and consent steps in your normal browser. The
   loopback callback checks the random state and exchanges the code with its
   original PKCE verifier. It expires after five minutes and supports Cancel.
6. The account must have a valid Java Edition entitlement and Minecraft profile.
   Xbox family/age/region errors produce instructions rather than a guest launch.

Keep this client ID in the desktop distribution or user settings; the server
panel never needs account credentials. Every Play refreshes the account and
checks ownership online. Rotated refresh tokens replace the previous encrypted
token. Revoked refresh tokens require a new sign-in. Account switching is through
the browser's account picker; only one account is saved per data directory.

## Minecraft installation and process behavior

Game files are isolated under `<instance>/.launcher/minecraft`; worlds, mods,
options, and Minecraft's own logs use `<instance>`. The ordinary official launcher
directory is not modified. Installers run in a separate child process so Cancel
can stop the download/installer process tree. Pack updates remain transactional;
game downloads are repairable caches, not part of that pack transaction. A failed
game install preserves the completed pack and removes the runtime success receipt.
Run **Install / repair** again to verify and complete it before Play.

The pinned library uses upstream version metadata and checksums for official game
files and selects native libraries for the current platform. The worker requires
HTTPS and applies network timeouts. Mod-loader installers run downloaded Java
code from their upstream distribution. Use trusted catalogs and mods. Cancellation
does not undo already downloaded runtime cache files. A custom Java selection
skips the library's automatic Java download, useful on unsupported runtime platforms.

The runtime receipt is `.launcher/game.json`. Play requires matching pack/runtime
receipts and existing classpath files, checks the executable's Java major, and
holds the installation lock until Minecraft exits. A second launcher cannot
update or launch that instance while this one supervises its game. Close the
game normally to save progress; **Stop game** forcibly ends it after confirmation.
Closing the launcher during play also asks before stopping Minecraft. Manually
started games, editors, and a game left behind by a launcher crash are outside
this lock; close those before updates.

The launcher captures up to 8 MiB of output in `.launcher/game-output.log`,
redacting its game access token and common token arguments. It never prints the
launch command. The JVM receives the access token through Minecraft's standard
launch arguments, which local process-inspection tools can see. Minecraft/mods
also write their own logs independently. A nonzero exit status points to the
captured output for diagnosis.

## Client CLI

From the project environment (or after installing the project):

```powershell
# Works directly from this checkout without reinstalling entry points:
.\.venv\Scripts\python.exe -m mc_manager.launcher.cli --catalog .\launcher-catalog.json list
.\.venv\Scripts\python.exe -m mc_manager.launcher.cli --catalog .\launcher-catalog.json plan --profile monkeycraft-vanilla --instance .\instances\monkeycraft-vanilla

# Entry point available after pip install -e .:
mc-manager-launcher --catalog https://downloads.example.net/launcher-catalog.json list
mc-manager-launcher --catalog .\launcher-catalog.json install --profile monkeycraft-vanilla --instance .\instances\monkeycraft-vanilla
mc-manager-launcher recover --instance .\instances\monkeycraft-vanilla

# Full game install and authenticated play (login persistence requires Windows):
mc-manager-launcher --client-id <your-public-client-id> login
mc-manager-launcher check-account
mc-manager-launcher --catalog .\launcher-catalog.json status --profile monkeycraft-vanilla --instance .\instances\monkeycraft-vanilla
mc-manager-launcher --catalog .\launcher-catalog.json install-game --profile monkeycraft-vanilla --instance .\instances\monkeycraft-vanilla
mc-manager-launcher --catalog .\launcher-catalog.json play --profile monkeycraft-vanilla --instance .\instances\monkeycraft-vanilla --memory 4096
mc-manager-launcher logout
```

`--catalog` takes a local JSON file or the full catalog URL. Plain HTTP requires
`--allow-http` for a trusted development network. The CLI sends no panel password,
agent token, Microsoft token, or account data to the catalog server. Sign-in and
Play communicate directly with Microsoft, Xbox, and Minecraft. Public player catalogs should be
served over HTTPS from a trusted host; file hashes establish integrity against
that catalog, not an independent signature of its publisher.

`plan` reads managed paths in the desired release and the previous installation
receipt. It verifies size/SHA-256, returns `download`, `replace`, or `keep` for
desired files and a separate `removals` list for obsolete managed files, and totals
download bytes. `conflicts` identifies locally edited or unowned files that would
change. `plan_id` binds a review to that exact filesystem state. It refuses
links/junctions and files in place of required directories. Planning writes nothing.

`install` downloads all needed files to a unique staging directory before changing
the live pack, verifies exact sizes and SHA-256, rechecks local files, and saves
backups before applying replacements/removals. HTTPS redirects are limited to five
hops and cannot switch to HTTP or credential-bearing URLs. Unowned files and
saves are preserved. Previously owned files no longer present in the manifest are
removed after backup. `--replace-modified` explicitly permits replacement/removal
of conflicting local files; otherwise the operation stops for review. An instance
cannot switch profile IDs or accept changed content under an installed release's
existing version number.

The `.launcher/installed.json` receipt records the installed manifest.
`.launcher/transaction.json` journals pending changes. Replacements use atomic
file renames; committing the receipt marks a completed transaction. Exceptions
and cancellation during application trigger rollback. A process interruption can
be recovered with **Recover** or the CLI's `recover` command; CLI installation also
recovers pending work before proceeding. Recovery refuses to overwrite newer local
edits or trust damaged backups, leaving the journal for investigation. OS file
locking prevents concurrent updates and is also held while the launcher runs a
game. It releases on process exit. External editors and manually started games
are outside the lock. This provides process-interruption recovery, not a
guarantee against storage failure or power loss on every filesystem.

Backups and staging files remain in `.launcher/history/<transaction-id>/` for
inspection. They currently have no automatic retention policy, including unused
staging data from failed downloads. Remove old history only while no installation
is running and no pending journal refers to it. Download and backup disk space is
checked before starting. The pack-only `plan`/`install` APIs retain
`launch_ready: false` because they do not inspect a game runtime or account.
`install-game` additionally returns `game_installed: true`; Play performs the
runtime/account checks at launch time.

For a desktop frontend, call `read_catalog(source)` followed by
`plan_installation(profile.release, instance_path)`. Apply a reviewed plan using
`install_release(..., expected_plan_id=plan["plan_id"])`, with a progress callback
and optional cancellation event. The desktop UI runs work on a background thread
and only updates Tk widgets on the main thread. Keep game/account work on the
player's machine; the panel only publishes client configuration.

## Next implementation milestones

1. Register/approve our Microsoft application and complete a live account login,
   entitlement check, and game-launch smoke test. No client ID was available during
   implementation, so authentication and process supervision were tested with
   simulated services/processes. Minecraft 1.21.1, Java 21, and NeoForge 21.1.200
   were installed live into an isolated test instance; their launch arguments and
   classpath files were checked without starting an unauthenticated game.
2. Packaging, signing, launcher updates, `.mrpack` import, and integration tests on
   clean Windows machines before publishing a player installer.
3. Download retry/resume, history retention, optional client catalog signatures,
   and persistent macOS/Linux account storage.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_launcher.py tests/test_launcher_installer.py tests/test_launcher_auth.py tests/test_launcher_game.py tests/test_panel.py tests/test_config.py -q
node --check mc_manager/static/launcher.js
```

Launcher tests cover role restrictions, opt-in visibility, immutable releases,
publication/rollback/withdrawal, persistence, concurrent publication conflicts,
private metadata exclusion, unsafe file paths/URLs, client hash checks, bounded
catalog downloads, CLI round trips, corrupt/truncated downloads, cancellation,
recovery, conflicting local changes, obsolete-file removal, redirects, disk-space
failures, concurrent instance locks, browser callback state/PKCE, account
renewal/revocation, entitlement failures, token encryption/redaction, exact loader
selection, launch arguments, Java mismatches, and running-game update protection.
No live server or Microsoft account is required for the automated suite. Install
the `launcher` extra to run tests against the real command-builder library.
