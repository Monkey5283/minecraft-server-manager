/* Administrator UI for the versioned launcher contract. */
(() => {
  const node = (id) => document.getElementById(`launcher-${id}`);
  const dialog = document.getElementById("launcher-dialog");
  const openButton = document.getElementById("open-launcher");
  let profiles = [];
  let servers = [];
  let selected = null;
  let releases = [];
  let selectionGeneration = 0;
  let sessionGeneration = 0;
  let busy = false;

  function message(text, error = false) {
    node("notice").textContent = text;
    node("notice").className = `notice ${error ? "error" : "success"}`;
    node("notice").hidden = false;
  }

  async function refreshAccess() {
    const generation = ++sessionGeneration;
    try {
      const session = await api("/api/session");
      if (generation !== sessionGeneration) return;
      openButton.hidden = !session.authenticated || !session.admin;
      if (openButton.hidden) dialog.close();
    } catch { openButton.hidden = true; }
  }
  window.addEventListener("panel-session-change", refreshAccess);
  refreshAccess();

  function renderProfiles() {
    node("profiles").replaceChildren();
    if (!profiles.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No profiles yet. Create one to begin.";
      node("profiles").append(empty);
    }
    for (const profile of profiles) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary launcher-profile-choice";
      button.setAttribute("aria-pressed", String(profile.id === selected?.id));
      const title = document.createElement("span");
      title.textContent = profile.name;
      const status = document.createElement("small");
      status.textContent = profile.active_version ? `Published: ${profile.active_version}` : "Not published";
      button.append(title, status);
      button.addEventListener("click", () => run(() => selectProfile(profile.id)));
      node("profiles").append(button);
    }
  }

  function updateLoader() {
    const modded = node("loader").value !== "vanilla";
    node("loader-version").disabled = !modded;
    node("loader-version").required = modded;
    if (!modded) node("loader-version").value = "";
  }

  function renderReleaseSelection() {
    const release = releases.find((item) => item.manifest.version === node("release-list").value);
    node("publish").disabled = !release || release.manifest.version === selected?.active_version;
    node("template").disabled = !release;
    node("withdraw").disabled = !selected?.active_version;
    node("release-summary").textContent = release
      ? `Minecraft ${release.manifest.minecraft_version} · ${release.manifest.loader.kind} · ${release.manifest.files.length} files · ${release.published_at ? "Previously published" : "Saved, not yet published"}`
      : "Create a release below to get started.";
  }

  async function selectProfile(id, preferredVersion = null) {
    const generation = ++selectionGeneration;
    selected = profiles.find((profile) => profile.id === id) || null;
    releases = [];
    node("profile-form").reset();
    node("release-form").reset();
    updateLoader();
    node("profile-heading").textContent = selected ? selected.name : "New profile";
    node("profile-id").value = selected?.id || "";
    node("profile-id").readOnly = Boolean(selected);
    node("profile-name").value = selected?.name || "";
    node("profile-description").value = selected?.description || "";
    node("profile-server").replaceChildren(new Option("No associated server", ""));
    for (const server of servers) node("profile-server").add(new Option(server.name, server.id));
    if (selected?.server_id && !servers.some((server) => server.id === selected.server_id)) {
      node("profile-server").add(new Option(`${selected.server_id} (no longer available)`, selected.server_id));
    }
    node("profile-server").value = selected?.server_id || "";
    node("release-section").hidden = !selected;
    node("release-list").replaceChildren();
    renderProfiles();
    renderReleaseSelection();
    if (!selected) return;
    node("current-release").textContent = selected.active_version
      ? `Players receive release ${selected.active_version}.` : "This profile is not in the player catalog.";
    const result = await api(`/api/launcher/profiles/${encodeURIComponent(id)}/releases`);
    if (generation !== selectionGeneration) return;
    releases = result;
    for (const item of releases) {
      const version = item.manifest.version;
      node("release-list").add(new Option(`${version}${version === selected.active_version ? " (current)" : ""}`, version));
    }
    if (preferredVersion || selected.active_version) node("release-list").value = preferredVersion || selected.active_version;
    renderReleaseSelection();
  }

  async function load(id = selected?.id, preferredVersion = null) {
    const result = await api("/api/launcher");
    profiles = result.profiles;
    servers = result.servers;
    const downloadSite = node("download-site");
    const publicUrl = result.download_url ? new URL(result.download_url) : null;
    downloadSite.hidden = !publicUrl || publicUrl.protocol !== "https:";
    if (!downloadSite.hidden) downloadSite.href = publicUrl.href;
    node("feed-status").textContent = result.public_catalog_enabled
      ? `Player catalog is available at ${new URL(result.catalog_path, location.origin).href}`
      : "Private controller: export the published catalog and upload it as catalog.json on the separate public download site. Keep this controller on the LAN/VPN; players do not need panel access.";
    await selectProfile(id, preferredVersion);
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    node("notice").hidden = true;
    const controls = [...dialog.querySelectorAll("button, input, select, textarea")];
    const disabled = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    try { await action(); }
    catch (error) { message(error.message, true); }
    finally {
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      busy = false;
      updateLoader();
      renderReleaseSelection();
    }
  }

  openButton.addEventListener("click", () => {
    dialog.showModal();
    run(() => load());
  });
  node("reload").addEventListener("click", () => run(() => load()));
  node("new-profile").addEventListener("click", () => run(() => selectProfile(null)));
  node("loader").addEventListener("change", updateLoader);
  node("release-list").addEventListener("change", renderReleaseSelection);

  node("profile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    run(async () => {
      const payload = {name: node("profile-name").value, description: node("profile-description").value,
        server_id: node("profile-server").value || null};
      const id = selected?.id || node("profile-id").value;
      await api(selected ? `/api/launcher/profiles/${encodeURIComponent(id)}` : "/api/launcher/profiles", {
        method: selected ? "PUT" : "POST",
        body: JSON.stringify(selected ? {...payload, expected_revision: selected.revision} : {...payload, id}),
      });
      await load(id);
      message("Profile saved.");
    });
  });

  node("release-form").addEventListener("submit", (event) => {
    event.preventDefault();
    run(async () => {
      if (!selected) return;
      let files;
      try { files = JSON.parse(node("files").value); }
      catch { throw new Error("Client files must be a valid JSON array."); }
      if (!Array.isArray(files)) throw new Error("Client files must be a JSON array.");
      const payload = {version: node("version").value, minecraft_version: node("minecraft").value,
        loader: {kind: node("loader").value, version: node("loader-version").value || null},
        java_major: Number(node("java").value), memory_mb: Number(node("memory").value),
        server_address: node("address").value || null, changelog: node("changelog").value, files};
      const result = await api(`/api/launcher/profiles/${encodeURIComponent(selected.id)}/releases`, {
        method: "POST", body: JSON.stringify(payload),
      });
      await load(selected.id, result.version);
      message(`Release ${result.version} saved. Publish it when it is ready for players.`);
    });
  });

  async function publish(version) {
    if (!selected) return;
    await api(`/api/launcher/profiles/${encodeURIComponent(selected.id)}/publication`, {
      method: "PUT", body: JSON.stringify({version, expected_revision: selected.revision}),
    });
    await load(selected.id);
    message(version ? `Release ${version} is now published.` : "Profile withdrawn from the player catalog.");
  }
  node("publish").addEventListener("click", () => run(() => publish(node("release-list").value)));
  node("withdraw").addEventListener("click", () => run(() => publish(null)));
  node("template").addEventListener("click", () => {
    const source = releases.find((item) => item.manifest.version === node("release-list").value)?.manifest;
    if (!source) return;
    node("version").value = "";
    node("minecraft").value = source.minecraft_version;
    node("loader").value = source.loader.kind;
    updateLoader();
    node("loader-version").value = source.loader.version || "";
    node("java").value = source.java_major;
    node("memory").value = source.memory_mb;
    node("address").value = source.server_address || "";
    node("changelog").value = "";
    node("files").value = JSON.stringify(source.files, null, 2);
    node("version").focus();
  });
})();
