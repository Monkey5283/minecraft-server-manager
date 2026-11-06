# Modpack administration

Open **Launcher & modpacks** from the private manager. Only administrators can
read or modify packs, review release changes, or export the catalog.

Create a pack in **Pack details**, then expand **Create a release**. Choose the
Minecraft version and exact loader version, Java requirement, memory, release
notes, and optional public game-server address. Folder import calculates hashes
locally for managed client directories only. Review configuration files before
including them. It excludes worlds and other unmanaged instance directories.

Files must already exist at the supplied HTTPS base URL. Folder import does not
upload them. Imports allow up to 4,096 files, each no larger than 256 MiB. Use an
empty file list for an unmodified vanilla client.

Save a version, then use **Review update changes** to inspect additions, changes,
removals, and runtime differences from the selected catalog release. **Use
selected release** changes the controller catalog; selecting a previous version
allows rollback. Export the catalog and deploy it as `catalog.json` to the
separate public download site to deliver the change to players. The same export
step is needed after withdrawing a profile.

Retained manifests share an 8 GB decimal file budget, including drafts and
withdrawn versions. Identical URL/checksum pairs count once. This is a catalog
budget, not a measurement or cap on Cloudflare billing. Automatic hosting uploads
and R2 provisioning are disabled to preserve the owner's free-tier-only setup.

The UI update changes no network bindings, authentication, Tailscale settings,
public catalog configuration, or game-server services. Keep the controller bound
to loopback, served through Tailscale Serve, with Funnel disabled and
`launcher.public_catalog_enabled = false`. Public downloads remain on Pages.

Deploy using **Account & manager → Update controller** after the reviewed release
is merged into the updater's branch. This briefly restarts the control panel;
the game servers do not need restarting. The existing updater preserves config
and rolls its code back if the controller fails to start.
