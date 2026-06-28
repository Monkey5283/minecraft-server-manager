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

const actionLabels = {
  start: "Start",
  stop: "Stop",
  restart: "Restart",
  update: "Apply update",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
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
  refreshButton.hidden = true;
  logoutButton.hidden = true;
}

function showDashboard() {
  loginPanel.hidden = true;
  dashboard.hidden = false;
  refreshButton.hidden = false;
  logoutButton.hidden = false;
}

function showNotice(message, kind = "info") {
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
  window.setTimeout(() => (notice.hidden = true), 6000);
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
