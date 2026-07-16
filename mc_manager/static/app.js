const loginPanel = document.querySelector("#login-panel");
const dashboard = document.querySelector("#dashboard");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const serversNode = document.querySelector("#servers");
const template = document.querySelector("#server-template");
const refreshButton = document.querySelector("#refresh");
const logoutButton = document.querySelector("#logout");
const manageServersButton = document.querySelector("#manage-servers");
const lastUpdated = document.querySelector("#last-updated");
const notice = document.querySelector("#notice");
const serverRegistry = document.querySelector("#server-registry");
const closeServerRegistryButton = document.querySelector("#close-server-registry");
const discoverServersButton = document.querySelector("#discover-servers");
const registryNotice = document.querySelector("#registry-notice");
const configuredServers = document.querySelector("#configured-servers");
const discoveredServers = document.querySelector("#discovered-servers");
const fileManager = document.querySelector("#file-manager");
const fileServerName = document.querySelector("#file-server-name");
const fileNotice = document.querySelector("#file-notice");
const breadcrumbs = document.querySelector("#file-breadcrumbs");
const fileList = document.querySelector("#file-list");
const emptyDirectory = document.querySelector("#empty-directory");
const closeFilesButton = document.querySelector("#close-files");
const newFileButton = document.querySelector("#new-file");
const newFolderButton = document.querySelector("#new-folder");
const uploadButton = document.querySelector("#upload-file");
const uploadInput = document.querySelector("#upload-input");
const editor = document.querySelector("#file-editor");
const editorFileName = document.querySelector("#editor-file-name");
const editorContent = document.querySelector("#editor-content");
const editorStatus = document.querySelector("#editor-status");
const closeEditorButton = document.querySelector("#close-editor");
const saveFileButton = document.querySelector("#save-file");

let activeFileServer = null;
let currentDirectory = "";
let currentFileEntries = [];
let currentFileLimits = { max_edit_size_bytes: 0, max_upload_size_bytes: 0 };
let openDocument = null;

const actionLabels = {
  start: "Start",
  stop: "Stop",
  restart: "Restart",
  update: "Apply update",
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    ...options,
    headers,
  });
  let body = {};
  try {
    body = await response.json();
  } catch {}
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

function showLogin() {
  loginPanel.hidden = false;
  dashboard.hidden = true;
  fileManager.hidden = true;
  serverRegistry.hidden = true;
  manageServersButton.hidden = true;
  refreshButton.hidden = true;
  logoutButton.hidden = true;
}

function showDashboard() {
  loginPanel.hidden = true;
  dashboard.hidden = false;
  fileManager.hidden = true;
  manageServersButton.hidden = false;
  refreshButton.hidden = false;
  logoutButton.hidden = false;
}

function showNotice(message, kind = "info") {
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
  window.setTimeout(() => (notice.hidden = true), 6000);
}

function showFileNotice(message, kind = "info") {
  fileNotice.textContent = message;
  fileNotice.className = `notice ${kind}`;
  fileNotice.hidden = false;
  window.setTimeout(() => (fileNotice.hidden = true), 7000);
}

function showRegistryNotice(message, kind = "info") {
  registryNotice.textContent = message;
  registryNotice.className = `notice ${kind}`;
  registryNotice.hidden = false;
  window.setTimeout(() => (registryNotice.hidden = true), 7000);
}

async function loadServers() {
  refreshButton.disabled = true;
  try {
    const servers = await api("/api/servers");
    serversNode.replaceChildren(...servers.map(renderServer));
    lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (error.message === "Sign in required") showLogin();
    else showNotice(error.message, "error");
  } finally {
    refreshButton.disabled = false;
  }
}

function renderServer(server) {
  const card = template.content.firstElementChild.cloneNode(true);
  card.dataset.serverId = server.controller_id;
  card.querySelector(".server-id").textContent = server.controller_id;
  card.querySelector(".server-name").textContent = server.name;
  const state = card.querySelector(".state");
  state.textContent = server.state;
  state.dataset.state = server.state;
  const detail = card.querySelector(".detail");
  detail.textContent =
    server.state === "unreachable" ? server.detail : "Agent connected and responding.";

  const actions = card.querySelector(".actions");
  for (const action of server.actions) {
    const button = document.createElement("button");
    button.textContent = actionLabels[action] || action;
    button.dataset.action = action;
    if (action === "stop" || action === "update") button.className = "warning";
    button.addEventListener("click", () => runAction(card, server, action));
    actions.append(button);
  }

  if (server.scripts.length) {
    const scriptPanel = card.querySelector(".scripts");
    const select = scriptPanel.querySelector("select");
    for (const name of server.scripts) {
      select.add(new Option(name, name));
    }
    scriptPanel.querySelector(".run-script").addEventListener("click", () => {
      runAction(card, server, "script", select.value);
    });
    scriptPanel.hidden = false;
  }
  if (server.files_enabled) {
    const filesButton = document.createElement("button");
    filesButton.textContent = "Manage files";
    filesButton.className = "secondary";
    filesButton.addEventListener("click", () => openFileManager(server));
    actions.append(filesButton);
  }
  return card;
}

function registryItemHeading(server, badgeText) {
  const heading = document.createElement("div");
  heading.className = "registry-item-heading";
  const identity = document.createElement("div");
  const serverId = document.createElement("p");
  serverId.className = "server-id";
  serverId.textContent = server.id;
  const name = document.createElement("h4");
  name.textContent = server.name;
  identity.append(serverId, name);
  const badge = document.createElement("span");
  badge.className = "state";
  badge.textContent = badgeText;
  heading.append(identity, badge);
  return heading;
}

function trackingControl(checked, available = true) {
  const label = document.createElement("label");
  label.className = "registry-checkbox";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = checked;
  checkbox.disabled = !available;
  const text = document.createElement("span");
  text.textContent = available
    ? "Track players for Discord status"
    : "Player tracking is not configured on this agent";
  label.append(checkbox, text);
  return { label, checkbox };
}

function renderConfiguredServer(server) {
  const item = document.createElement("article");
  item.className = "registry-item";
  item.append(registryItemHeading(server, server.managed ? "dashboard" : "config file"));

  if (!server.managed) {
    const detail = document.createElement("p");
    detail.className = "registry-item-detail";
    detail.textContent = "Protected base server. Edit controller.toml to change it.";
    item.append(detail);
    return item;
  }

  const nameLabel = document.createElement("label");
  nameLabel.textContent = "Display name";
  const nameInput = document.createElement("input");
  nameInput.value = server.name;
  nameInput.maxLength = 80;
  nameInput.required = true;
  nameLabel.append(nameInput);
  const tracking = trackingControl(server.track_players);
  const actions = document.createElement("div");
  actions.className = "registry-item-actions";
  const save = document.createElement("button");
  save.textContent = "Save";
  const remove = document.createElement("button");
  remove.textContent = "Remove";
  remove.className = "warning";
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await api(`/api/server-registry/${encodeURIComponent(server.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          name: nameInput.value,
          track_players: tracking.checkbox.checked,
        }),
      });
      showRegistryNotice(`Saved ${server.id}.`, "success");
      await Promise.all([loadServers(), loadServerRegistry(false)]);
    } catch (error) {
      showRegistryNotice(error.message, "error");
    } finally {
      save.disabled = false;
    }
  });
  remove.addEventListener("click", async () => {
    const confirmation = window.prompt(
      `Remove ${server.name} from this dashboard? Type ${server.id} to confirm.`
    );
    if (confirmation === null) return;
    remove.disabled = true;
    try {
      await api(`/api/server-registry/${encodeURIComponent(server.id)}/remove`, {
        method: "POST",
        body: JSON.stringify({ confirm_id: confirmation }),
      });
      showRegistryNotice(`Removed ${server.id} from the dashboard.`, "success");
      await Promise.all([loadServers(), loadServerRegistry(true)]);
    } catch (error) {
      showRegistryNotice(error.message, "error");
    } finally {
      remove.disabled = false;
    }
  });
  actions.append(save, remove);
  item.append(nameLabel, tracking.label, actions);
  return item;
}

function renderDiscoveredServer(server) {
  const item = document.createElement("article");
  item.className = "registry-item";
  item.append(registryItemHeading(server, server.state));
  const detail = document.createElement("p");
  detail.className = "registry-item-detail";
  detail.textContent = `${server.files_enabled ? "File manager ready" : "File manager unavailable"} · via ${server.source_name}`;
  const nameLabel = document.createElement("label");
  nameLabel.textContent = "Dashboard name";
  const nameInput = document.createElement("input");
  nameInput.value = server.name;
  nameInput.maxLength = 80;
  nameInput.required = true;
  nameLabel.append(nameInput);
  const tracking = trackingControl(false, server.player_tracking_available);
  const add = document.createElement("button");
  add.textContent = "Add to dashboard";
  add.addEventListener("click", async () => {
    add.disabled = true;
    try {
      await api("/api/server-registry", {
        method: "POST",
        body: JSON.stringify({
          server_id: server.id,
          source_server_id: server.source_server_id,
          name: nameInput.value,
          track_players: tracking.checkbox.checked,
        }),
      });
      showRegistryNotice(`Added ${server.id} to the dashboard.`, "success");
      await Promise.all([loadServers(), loadServerRegistry(true)]);
    } catch (error) {
      showRegistryNotice(error.message, "error");
    } finally {
      add.disabled = false;
    }
  });
  item.append(detail, nameLabel, tracking.label, add);
  return item;
}

function emptyRegistryMessage(message) {
  const paragraph = document.createElement("p");
  paragraph.className = "empty-state registry-empty";
  paragraph.textContent = message;
  return paragraph;
}

async function loadServerRegistry(scanAgents = true) {
  discoverServersButton.disabled = true;
  serverRegistry.setAttribute("aria-busy", "true");
  try {
    const registryRequest = api("/api/server-registry");
    const discoveryRequest = scanAgents
      ? api("/api/server-registry/discover")
      : Promise.resolve(null);
    const [registry, discovery] = await Promise.all([registryRequest, discoveryRequest]);
    configuredServers.replaceChildren(
      ...registry.configured.map(renderConfiguredServer)
    );
    if (discovery) {
      const candidates = discovery.candidates.map(renderDiscoveredServer);
      if (!candidates.length) {
        candidates.push(
          emptyRegistryMessage("No unregistered servers were found on the trusted agents.")
        );
      }
      discoveredServers.replaceChildren(...candidates);
      if (discovery.unavailable.length) {
        showRegistryNotice(
          `${discovery.unavailable.length} agent connection could not be scanned.`,
          "error"
        );
      }
    }
  } catch (error) {
    showRegistryNotice(error.message, "error");
  } finally {
    serverRegistry.removeAttribute("aria-busy");
    discoverServersButton.disabled = false;
  }
}

async function openServerRegistry() {
  serverRegistry.hidden = false;
  serverRegistry.scrollIntoView({ behavior: "smooth", block: "start" });
  await loadServerRegistry(true);
}

async function runAction(card, server, action, scriptName = "") {
  const description = action === "script" ? `run '${scriptName}'` : action;
  if ((action === "stop" || action === "update") &&
      !window.confirm(`Really ${description} ${server.name}?`)) return;

  setCardBusy(card, true, `Starting ${description}…`);
  try {
    const path =
      action === "script"
        ? `/api/servers/${server.controller_id}/scripts/${encodeURIComponent(scriptName)}`
        : `/api/servers/${server.controller_id}/actions/${action}`;
    const job = await api(path, { method: "POST" });
    await watchJob(card, server, job.id);
  } catch (error) {
    setCardBusy(card, false);
    showNotice(error.message, "error");
  }
}

async function watchJob(card, server, jobId) {
  for (;;) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    const job = await api(
      `/api/servers/${server.controller_id}/jobs/${encodeURIComponent(jobId)}`
    );
    card.querySelector(".job-text").textContent = `${job.operation}: ${job.state}`;
    if (job.state === "succeeded") {
      setCardBusy(card, false);
      showNotice(`${server.name}: ${job.operation} completed.`, "success");
      await loadServers();
      return;
    }
    if (job.state === "failed") {
      setCardBusy(card, false);
      showNotice(`${server.name}: ${job.error || "operation failed"}`, "error");
      return;
    }
  }
}

function setCardBusy(card, busy, message = "") {
  card.querySelectorAll("button, select").forEach((item) => (item.disabled = busy));
  const jobNode = card.querySelector(".job");
  jobNode.hidden = !busy;
  if (busy) jobNode.querySelector(".job-text").textContent = message;
}

function joinFilePath(directory, name) {
  return directory ? `${directory}/${name}` : name;
}

function parentDirectory(path) {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function validEntryName(name) {
  return Boolean(name) && name !== "." && name !== ".." &&
    !name.includes("/") && !name.includes("\\");
}

function formatBytes(size) {
  if (size === null || size === undefined) return "—";
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB"];
  let value = size;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[unit]}`;
}

function editorIsDirty() {
  return openDocument !== null && editorContent.value !== openDocument.content;
}

function confirmDiscardEditor() {
  return !editorIsDirty() || window.confirm("Discard unsaved file changes?");
}

async function openFileManager(server) {
  activeFileServer = server;
  currentDirectory = "";
  currentFileEntries = [];
  openDocument = null;
  editor.hidden = true;
  fileServerName.textContent = server.name;
  dashboard.hidden = true;
  loginPanel.hidden = true;
  fileManager.hidden = false;
  manageServersButton.hidden = true;
  refreshButton.hidden = true;
  await loadDirectory("");
}

async function leaveFileManager() {
  if (!confirmDiscardEditor()) return;
  activeFileServer = null;
  openDocument = null;
  editor.hidden = true;
  showDashboard();
  await loadServers();
}

function renderBreadcrumbs() {
  breadcrumbs.replaceChildren();
  const rootButton = document.createElement("button");
  rootButton.className = "breadcrumb secondary";
  rootButton.textContent = activeFileServer.name;
  rootButton.addEventListener("click", () => navigateDirectory(""));
  breadcrumbs.append(rootButton);
  const parts = currentDirectory.split("/").filter(Boolean);
  parts.forEach((part, index) => {
    const separator = document.createElement("span");
    separator.textContent = "/";
    breadcrumbs.append(separator);
    const button = document.createElement("button");
    button.className = "breadcrumb secondary";
    button.textContent = part;
    button.addEventListener("click", () =>
      navigateDirectory(parts.slice(0, index + 1).join("/"))
    );
    breadcrumbs.append(button);
  });
}

async function navigateDirectory(path) {
  if (!confirmDiscardEditor()) return;
  openDocument = null;
  editor.hidden = true;
  await loadDirectory(path);
}

async function loadDirectory(path) {
  if (!activeFileServer) return;
  fileList.setAttribute("aria-busy", "true");
  try {
    const listing = await api(
      `/api/servers/${activeFileServer.controller_id}/files?path=${encodeURIComponent(path)}`
    );
    currentDirectory = listing.path;
    currentFileEntries = listing.entries;
    currentFileLimits = listing;
    renderBreadcrumbs();
    renderFileList();
  } catch (error) {
    showFileNotice(error.message, "error");
  } finally {
    fileList.removeAttribute("aria-busy");
  }
}

function renderFileList() {
  fileList.replaceChildren();
  if (currentDirectory) {
    fileList.append(createFileRow({
      name: "..",
      kind: "directory",
      path: parentDirectory(currentDirectory),
      size: null,
      modified_ms: null,
      editable: false,
    }));
  }
  for (const entry of currentFileEntries) fileList.append(createFileRow(entry));
  emptyDirectory.hidden = currentFileEntries.length > 0 || Boolean(currentDirectory);
}

function createFileRow(entry) {
  const row = document.createElement("div");
  row.className = "file-row";
  const nameButton = document.createElement("button");
  nameButton.className = "file-name-button";
  nameButton.textContent = `${entry.kind === "directory" ? "▸" : "◇"} ${entry.name}`;
  if (entry.kind === "directory") {
    nameButton.addEventListener("click", () => navigateDirectory(entry.path));
  } else if (entry.editable) {
    nameButton.addEventListener("click", () => openFile(entry.path));
  } else {
    nameButton.disabled = true;
    nameButton.title = "This file exceeds the text-editor size limit.";
  }
  const size = document.createElement("span");
  size.textContent = formatBytes(entry.size);
  const modified = document.createElement("span");
  modified.textContent = entry.modified_ms
    ? new Date(entry.modified_ms).toLocaleString()
    : "—";
  row.append(nameButton, size, modified);
  return row;
}

async function openFile(path) {
  if (!confirmDiscardEditor()) return;
  try {
    const file = await api(
      `/api/servers/${activeFileServer.controller_id}/files/content?path=${encodeURIComponent(path)}`
    );
    openDocument = { path: file.path, version: file.version, content: file.content };
    editorFileName.textContent = file.path;
    editorContent.value = file.content;
    editorStatus.textContent = `${formatBytes(file.size)} · UTF-8 text`;
    editor.hidden = false;
    editorContent.focus();
  } catch (error) {
    showFileNotice(error.message, "error");
  }
}

function beginNewFile() {
  if (!confirmDiscardEditor()) return;
  const name = window.prompt("New file name");
  if (name === null) return;
  if (!validEntryName(name)) {
    showFileNotice("Enter a file name without slashes.", "error");
    return;
  }
  const path = joinFilePath(currentDirectory, name);
  if (currentFileEntries.some((entry) => entry.name === name)) {
    showFileNotice("A file or directory with that name already exists.", "error");
    return;
  }
  openDocument = { path, version: null, content: "" };
  editorFileName.textContent = path;
  editorContent.value = "";
  editorStatus.textContent = "New UTF-8 text file";
  editor.hidden = false;
  editorContent.focus();
}

async function createFolder() {
  const name = window.prompt("New folder name");
  if (name === null) return;
  if (!validEntryName(name)) {
    showFileNotice("Enter a folder name without slashes.", "error");
    return;
  }
  try {
    await api(`/api/servers/${activeFileServer.controller_id}/files/directory`, {
      method: "POST",
      body: JSON.stringify({ path: joinFilePath(currentDirectory, name) }),
    });
    showFileNotice(`Created folder ${name}.`, "success");
    await loadDirectory(currentDirectory);
  } catch (error) {
    showFileNotice(error.message, "error");
  }
}

async function saveOpenFile() {
  if (!openDocument) return;
  saveFileButton.disabled = true;
  editorStatus.textContent = "Saving…";
  try {
    const saved = await api(
      `/api/servers/${activeFileServer.controller_id}/files/content`,
      {
        method: "PUT",
        body: JSON.stringify({
          path: openDocument.path,
          content: editorContent.value,
          expected_version: openDocument.version,
        }),
      }
    );
    openDocument.version = saved.version;
    openDocument.content = editorContent.value;
    editorStatus.textContent = `${formatBytes(saved.size)} · saved`;
    showFileNotice(`Saved ${saved.path}.`, "success");
    const savedPath = saved.path;
    await loadDirectory(currentDirectory);
    await openFile(savedPath);
  } catch (error) {
    editorStatus.textContent = "Not saved";
    showFileNotice(error.message, "error");
  } finally {
    saveFileButton.disabled = false;
  }
}

async function uploadSelectedFile() {
  const file = uploadInput.files[0];
  uploadInput.value = "";
  if (!file) return;
  if (!validEntryName(file.name)) {
    showFileNotice("That upload filename is not supported.", "error");
    return;
  }
  if (file.size > currentFileLimits.max_upload_size_bytes) {
    showFileNotice(
      `Upload is ${formatBytes(file.size)}; the limit is ${formatBytes(currentFileLimits.max_upload_size_bytes)}.`,
      "error"
    );
    return;
  }
  const exists = currentFileEntries.some((entry) => entry.name === file.name);
  if (exists && !window.confirm(`Overwrite ${file.name}?`)) return;
  uploadButton.disabled = true;
  try {
    const content = await file.arrayBuffer();
    const target = joinFilePath(currentDirectory, file.name);
    await api(
      `/api/servers/${activeFileServer.controller_id}/files/upload?path=${encodeURIComponent(target)}&overwrite=${exists}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: content,
      }
    );
    showFileNotice(`Uploaded ${file.name}.`, "success");
    await loadDirectory(currentDirectory);
  } catch (error) {
    showFileNotice(error.message, "error");
  } finally {
    uploadButton.disabled = false;
  }
}

closeFilesButton.addEventListener("click", leaveFileManager);
newFileButton.addEventListener("click", beginNewFile);
newFolderButton.addEventListener("click", createFolder);
uploadButton.addEventListener("click", () => uploadInput.click());
uploadInput.addEventListener("change", uploadSelectedFile);
saveFileButton.addEventListener("click", saveOpenFile);
closeEditorButton.addEventListener("click", () => {
  if (!confirmDiscardEditor()) return;
  openDocument = null;
  editor.hidden = true;
});
editorContent.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    saveOpenFile();
  }
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.querySelector("#username").value,
        password: document.querySelector("#password").value,
      }),
    });
    loginForm.reset();
    showDashboard();
    await loadServers();
  } catch (error) {
    loginError.textContent = error.message;
  }
});

refreshButton.addEventListener("click", loadServers);
manageServersButton.addEventListener("click", openServerRegistry);
discoverServersButton.addEventListener("click", () => loadServerRegistry(true));
closeServerRegistryButton.addEventListener("click", () => {
  serverRegistry.hidden = true;
});
logoutButton.addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

(async function start() {
  const session = await api("/api/session");
  if (session.authenticated) {
    showDashboard();
    await loadServers();
  } else {
    showLogin();
  }
})();
