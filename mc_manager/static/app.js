const loginPanel = document.querySelector("#login-panel");
const dashboard = document.querySelector("#dashboard");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const serversNode = document.querySelector("#servers");
const template = document.querySelector("#server-template");
const refreshButton = document.querySelector("#refresh");
const logoutButton = document.querySelector("#logout");
const lastUpdated = document.querySelector("#last-updated");
const notice = document.querySelector("#notice");
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
const saveAndRestartButton = document.querySelector("#save-and-restart");
const restartFromFilesButton = document.querySelector("#restart-from-files");
const consolePanel = document.querySelector("#console-panel");
const consoleServerName = document.querySelector("#console-server-name");
const consoleNotice = document.querySelector("#console-notice");
const consoleOutput = document.querySelector("#console-output");
const consoleForm = document.querySelector("#console-form");
const consoleCommand = document.querySelector("#console-command");
const closeConsoleButton = document.querySelector("#close-console");
const openSetupButton = document.querySelector("#open-setup");
const setupPanel = document.querySelector("#setup-panel");
const closeSetupButton = document.querySelector("#close-setup");
const setupNotice = document.querySelector("#setup-notice");
const discoveredAgentsNode = document.querySelector("#discovered-agents");
const scanAgentsButton = document.querySelector("#scan-agents");
const provisionForm = document.querySelector("#provision-form");
const provisionAgent = document.querySelector("#provision-agent");
const serverType = document.querySelector("#server-type");
const serverVersion = document.querySelector("#server-version");
const serverVersionOptions = document.querySelector("#server-version-options");
const loadVersionsButton = document.querySelector("#load-versions");
const provisionJob = document.querySelector("#provision-job");

let activeFileServer = null;
let currentDirectory = "";
let currentFileEntries = [];
let currentFileLimits = { max_edit_size_bytes: 0, max_upload_size_bytes: 0 };
let openDocument = null;
let activeConsoleServer = null;
let consoleCursor = 0;
let consolePollGeneration = 0;

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
  stopConsolePolling();
  loginPanel.hidden = false;
  dashboard.hidden = true;
  fileManager.hidden = true;
  consolePanel.hidden = true;
  setupPanel.hidden = true;
  refreshButton.hidden = true;
  logoutButton.hidden = true;
  openSetupButton.hidden = true;
}

function showDashboard() {
  stopConsolePolling();
  loginPanel.hidden = true;
  dashboard.hidden = false;
  fileManager.hidden = true;
  consolePanel.hidden = true;
  setupPanel.hidden = true;
  refreshButton.hidden = false;
  logoutButton.hidden = false;
  openSetupButton.hidden = false;
}

function showSetupNotice(message, kind = "info") {
  setupNotice.textContent = message;
  setupNotice.className = `notice ${kind}`;
  setupNotice.hidden = false;
  window.setTimeout(() => (setupNotice.hidden = true), 8000);
}

async function openSetup() {
  stopConsolePolling();
  dashboard.hidden = true;
  fileManager.hidden = true;
  consolePanel.hidden = true;
  setupPanel.hidden = false;
  refreshButton.hidden = true;
  openSetupButton.hidden = true;
  await loadAgents();
}

async function loadAgents() {
  scanAgentsButton.disabled = true;
  try {
    const [discovered, paired] = await Promise.all([
      api("/api/agents/discovered"),
      api("/api/agents"),
    ]);
    renderDiscoveredAgents(discovered);
    const previous = provisionAgent.value;
    provisionAgent.replaceChildren(
      new Option(paired.length ? "Choose an agent" : "Pair an agent first", "")
    );
    for (const agent of paired) {
      provisionAgent.add(new Option(`${agent.name} (${agent.url})`, agent.id));
    }
    if ([...provisionAgent.options].some((option) => option.value === previous)) {
      provisionAgent.value = previous;
    }
  } catch (error) {
    showSetupNotice(error.message, "error");
  } finally {
    scanAgentsButton.disabled = false;
  }
}

function renderDiscoveredAgents(discovered) {
  discoveredAgentsNode.replaceChildren();
  if (!discovered.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No agents seen yet. Confirm the agent service is running and UDP 8765 is allowed on the LAN.";
    discoveredAgentsNode.append(empty);
    return;
  }
  for (const agent of discovered) {
    const row = document.createElement("div");
    row.className = "agent-row";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = agent.name;
    const address = document.createElement("span");
    address.textContent = agent.url;
    identity.append(title, address);
    row.append(identity);
    if (agent.paired) {
      const paired = document.createElement("span");
      paired.className = "state";
      paired.dataset.state = "online";
      paired.textContent = "Paired";
      row.append(paired);
    } else {
      const token = document.createElement("input");
      token.type = "password";
      token.placeholder = "Agent token";
      token.autocomplete = "off";
      token.setAttribute("aria-label", `Token for ${agent.name}`);
      const button = document.createElement("button");
      button.textContent = "Pair";
      button.addEventListener("click", async () => {
        if (!token.value.trim()) {
          showSetupNotice("Paste the token from the agent first.", "error");
          return;
        }
        button.disabled = true;
        try {
          await api("/api/agents/pair", {
            method: "POST",
            body: JSON.stringify({ agent_id: agent.id, token: token.value.trim() }),
          });
          token.value = "";
          showSetupNotice(`Paired ${agent.name}.`, "success");
          await loadAgents();
        } catch (error) {
          showSetupNotice(error.message, "error");
        } finally {
          button.disabled = false;
        }
      });
      const controls = document.createElement("div");
      controls.className = "pair-controls";
      controls.append(token, button);
      row.append(controls);
    }
    discoveredAgentsNode.append(row);
  }
}

async function loadVersions() {
  const agentId = provisionAgent.value;
  if (!agentId) {
    showSetupNotice("Choose a paired agent first.", "error");
    return;
  }
  loadVersionsButton.disabled = true;
  serverVersion.value = "";
  serverVersion.placeholder = "Loading publisher catalogâ€¦";
  serverVersionOptions.replaceChildren();
  try {
    const catalog = await api(
      `/api/agents/${encodeURIComponent(agentId)}/catalog/${encodeURIComponent(serverType.value)}`
    );
    serverVersion.placeholder = "Choose or enter a version";
    for (const version of catalog.versions) {
      const option = document.createElement("option");
      option.value = version.id;
      option.label = version.label;
      serverVersionOptions.append(option);
    }
  } catch (error) {
    serverVersion.placeholder = "Enter an exact version";
    showSetupNotice(error.message, "error");
  } finally {
    loadVersionsButton.disabled = false;
  }
}

async function watchProvisionJob(agentId, jobId) {
  provisionJob.hidden = false;
  provisionForm.querySelectorAll("button, input, select").forEach((node) => (node.disabled = true));
  try {
    for (;;) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      const job = await api(
        `/api/agents/${encodeURIComponent(agentId)}/jobs/${encodeURIComponent(jobId)}`
      );
      provisionJob.querySelector(".job-text").textContent = `Installation: ${job.state}`;
      if (job.state === "succeeded") {
        showSetupNotice("Server installed. It is now available on the dashboard.", "success");
        provisionForm.reset();
        serverVersion.value = "";
        serverVersionOptions.replaceChildren();
        await loadAgents();
        return;
      }
      if (job.state === "failed") throw new Error(job.error || "Installation failed");
    }
  } catch (error) {
    showSetupNotice(error.message, "error");
  } finally {
    provisionJob.hidden = true;
    provisionForm.querySelectorAll("button, input, select").forEach((node) => (node.disabled = false));
  }
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

function showConsoleNotice(message, kind = "info") {
  consoleNotice.textContent = message;
  consoleNotice.className = `notice ${kind}`;
  consoleNotice.hidden = false;
  window.setTimeout(() => (consoleNotice.hidden = true), 7000);
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
  if (server.console_enabled) {
    const consoleButton = document.createElement("button");
    consoleButton.textContent = "Open console";
    consoleButton.className = "secondary";
    consoleButton.addEventListener("click", () => openConsole(server));
    actions.append(consoleButton);
  }
  return card;
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
  stopConsolePolling();
  activeFileServer = server;
  currentDirectory = "";
  currentFileEntries = [];
  openDocument = null;
  editor.hidden = true;
  fileServerName.textContent = server.name;
  dashboard.hidden = true;
  loginPanel.hidden = true;
  fileManager.hidden = false;
  consolePanel.hidden = true;
  refreshButton.hidden = true;
  openSetupButton.hidden = true;
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
  size.className = "file-size";
  size.textContent = formatBytes(entry.size);
  const modified = document.createElement("span");
  modified.className = "file-modified";
  modified.textContent = entry.modified_ms
    ? new Date(entry.modified_ms).toLocaleString()
    : "—";
  const entryActions = document.createElement("div");
  entryActions.className = "file-entry-actions";
  if (entry.kind === "file") {
    const downloadButton = document.createElement("button");
    downloadButton.className = "secondary file-download";
    downloadButton.textContent = "Download";
    downloadButton.addEventListener("click", () => downloadFile(entry));
    entryActions.append(downloadButton);
  }
  if (entry.name !== "..") {
    const deleteButton = document.createElement("button");
    deleteButton.className = "warning file-delete";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", () => deleteFileEntry(entry));
    entryActions.append(deleteButton);
  }
  row.append(nameButton, size, modified, entryActions);
  return row;
}

async function deleteFileEntry(entry) {
  const confirmation = window.prompt(
    `Delete ${entry.path}? This cannot be undone. Type ${entry.name} to confirm.`
  );
  if (confirmation === null) return;
  if (confirmation !== entry.name) {
    showFileNotice("The file name confirmation did not match.", "error");
    return;
  }
  try {
    await api(
      `/api/servers/${activeFileServer.controller_id}/files?path=${encodeURIComponent(entry.path)}`,
      { method: "DELETE" }
    );
    if (openDocument?.path === entry.path) {
      openDocument = null;
      editor.hidden = true;
    }
    showFileNotice(`Deleted ${entry.path}.`, "success");
    await loadDirectory(currentDirectory);
  } catch (error) {
    showFileNotice(error.message, "error");
  }
}

function downloadFile(entry) {
  const link = document.createElement("a");
  link.href =
    `/api/servers/${activeFileServer.controller_id}/files/download?path=${encodeURIComponent(entry.path)}`;
  link.download = entry.name;
  document.body.append(link);
  link.click();
  link.remove();
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

async function saveOpenFile(restartAfterSave = false) {
  if (!openDocument) return;
  if (restartAfterSave && !window.confirm(
    `Save ${openDocument.path} and restart ${activeFileServer.name}?`
  )) return;
  saveFileButton.disabled = true;
  saveAndRestartButton.disabled = true;
  editorStatus.textContent = "Saving…";
  let savedSuccessfully = false;
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
    savedSuccessfully = true;
  } catch (error) {
    editorStatus.textContent = "Not saved";
    showFileNotice(error.message, "error");
  } finally {
    saveFileButton.disabled = false;
    saveAndRestartButton.disabled = false;
  }
  if (savedSuccessfully && restartAfterSave) await restartFileServer(true);
}

async function restartFileServer(alreadyConfirmed = false) {
  if (!activeFileServer) return;
  const server = activeFileServer;
  if (!alreadyConfirmed && !window.confirm(`Restart ${server.name} now?`)) return;
  restartFromFilesButton.disabled = true;
  showFileNotice(`Restarting ${server.name}…`);
  try {
    const job = await api(
      `/api/servers/${server.controller_id}/actions/restart`,
      { method: "POST" }
    );
    for (;;) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const result = await api(
        `/api/servers/${server.controller_id}/jobs/${encodeURIComponent(job.id)}`
      );
      if (result.state === "succeeded") {
        showFileNotice(`${server.name} restarted; saved changes are now active.`, "success");
        return;
      }
      if (result.state === "failed") {
        throw new Error(result.error || "Server restart failed");
      }
    }
  } catch (error) {
    showFileNotice(error.message, "error");
  } finally {
    restartFromFilesButton.disabled = false;
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

function stopConsolePolling() {
  consolePollGeneration += 1;
  activeConsoleServer = null;
}

async function openConsole(server) {
  stopConsolePolling();
  activeConsoleServer = server;
  consoleCursor = 0;
  consoleOutput.textContent = "";
  consoleServerName.textContent = server.name;
  loginPanel.hidden = true;
  dashboard.hidden = true;
  fileManager.hidden = true;
  setupPanel.hidden = true;
  consolePanel.hidden = false;
  refreshButton.hidden = true;
  openSetupButton.hidden = true;
  const generation = consolePollGeneration;
  await pollConsole(generation);
  consoleCommand.focus();
}

async function pollConsole(generation) {
  if (generation !== consolePollGeneration || !activeConsoleServer) return;
  const server = activeConsoleServer;
  try {
    const result = await api(
      `/api/servers/${server.controller_id}/console?cursor=${consoleCursor}`
    );
    if (result.reset) consoleOutput.textContent = "";
    if (result.content) {
      consoleOutput.textContent += result.content;
      if (consoleOutput.textContent.length > 1048576) {
        consoleOutput.textContent = consoleOutput.textContent.slice(-1048576);
      }
      consoleOutput.scrollTop = consoleOutput.scrollHeight;
    }
    consoleCursor = result.cursor;
  } catch (error) {
    showConsoleNotice(error.message, "error");
  }
  if (generation === consolePollGeneration && activeConsoleServer) {
    window.setTimeout(() => pollConsole(generation), 1000);
  }
}

consoleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activeConsoleServer) return;
  const command = consoleCommand.value.trim();
  if (!command) return;
  const submit = consoleForm.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    await api(`/api/servers/${activeConsoleServer.controller_id}/console`, {
      method: "POST",
      body: JSON.stringify({ command }),
    });
    consoleCommand.value = "";
  } catch (error) {
    showConsoleNotice(error.message, "error");
  } finally {
    submit.disabled = false;
    consoleCommand.focus();
  }
});

closeConsoleButton.addEventListener("click", async () => {
  showDashboard();
  await loadServers();
});

provisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const agentId = provisionAgent.value;
  if (!agentId || !serverVersion.value) {
    showSetupNotice("Choose an agent and load a server version.", "error");
    return;
  }
  const payload = {
    id: document.querySelector("#new-server-id").value,
    name: document.querySelector("#new-server-name").value,
    type: serverType.value,
    version: serverVersion.value,
    port: Number(document.querySelector("#new-server-port").value),
    java_path: document.querySelector("#java-path").value,
    minimum_memory: document.querySelector("#minimum-memory").value.toUpperCase(),
    maximum_memory: document.querySelector("#maximum-memory").value.toUpperCase(),
    accept_eula: document.querySelector("#accept-eula").checked,
  };
  try {
    const job = await api(`/api/agents/${encodeURIComponent(agentId)}/servers`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await watchProvisionJob(agentId, job.id);
  } catch (error) {
    showSetupNotice(error.message, "error");
  }
});

openSetupButton.addEventListener("click", openSetup);
closeSetupButton.addEventListener("click", async () => {
  showDashboard();
  await loadServers();
});
scanAgentsButton.addEventListener("click", loadAgents);
loadVersionsButton.addEventListener("click", loadVersions);
serverType.addEventListener("change", () => {
  serverVersion.value = "";
  serverVersionOptions.replaceChildren();
});

closeFilesButton.addEventListener("click", leaveFileManager);
newFileButton.addEventListener("click", beginNewFile);
newFolderButton.addEventListener("click", createFolder);
uploadButton.addEventListener("click", () => uploadInput.click());
uploadInput.addEventListener("change", uploadSelectedFile);
restartFromFilesButton.addEventListener("click", () => restartFileServer(false));
saveFileButton.addEventListener("click", () => saveOpenFile(false));
saveAndRestartButton.addEventListener("click", () => saveOpenFile(true));
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
