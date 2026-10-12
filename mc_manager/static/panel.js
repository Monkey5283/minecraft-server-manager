const panelNode = (id) => document.getElementById(id);
let scheduleServer = null;
let accountSession = null;
let updatePoll = null;

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => panelNode(button.dataset.close).close());
});
["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((day, index) => {
  const label = document.createElement("label");
  label.className = "checkbox-label";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = index;
  label.append(input, day);
  panelNode("schedule-days").append(label);
});

async function openRestartSchedule(server) {
  try {
    const schedule = await api(`/api/servers/${encodeURIComponent(server.controller_id)}/restart-schedule`);
    scheduleServer = server.controller_id;
    panelNode("schedule-title").textContent = `${server.name}: restart schedule`;
    panelNode("schedule-enabled").checked = schedule.enabled;
    panelNode("schedule-timezone").value = schedule.timezone;
    panelNode("schedule-times").value = schedule.times.join(", ");
    panelNode("schedule-days").querySelectorAll("input").forEach((input) => {
      input.checked = schedule.days.includes(Number(input.value));
    });
    panelNode("schedule-result").textContent = schedule.last_slot
      ? `Last scheduled run: ${schedule.last_slot} — ${schedule.last_result}` : "No scheduled runs yet.";
    panelNode("schedule-notice").hidden = true;
    panelNode("schedule-dialog").showModal();
  } catch (error) { showNotice(error.message, "error"); }
}

panelNode("schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const days = [...panelNode("schedule-days").querySelectorAll("input:checked")].map((input) => Number(input.value));
    if (!days.length) throw new Error("Select at least one day.");
    await api(`/api/servers/${encodeURIComponent(scheduleServer)}/restart-schedule`, {
      method: "PUT", body: JSON.stringify({
        enabled: panelNode("schedule-enabled").checked,
        timezone: panelNode("schedule-timezone").value.trim(), days,
        times: panelNode("schedule-times").value.split(",").map((time) => time.trim()),
      }),
    });
    showDialogNotice(panelNode("schedule-notice"), "Schedule saved.", "success");
  } catch (error) { showDialogNotice(panelNode("schedule-notice"), error.message, "error"); }
  finally { button.disabled = false; }
});

async function refreshUsers() {
  const users = await api("/api/users");
  panelNode("users-list").replaceChildren();
  for (const user of users) {
    const row = document.createElement("div");
    row.className = "agent-row";
    const identity = document.createElement("strong");
    identity.textContent = `${user.username} (${user.admin ? "Administrator" : "Operator"})`;
    row.append(identity);
    if (user.username !== accountSession.username) {
      const form = document.createElement("form");
      const password = document.createElement("input");
      password.type = "password";
      password.autocomplete = "new-password";
      password.minLength = 12;
      password.maxLength = 256;
      password.required = true;
      password.placeholder = "New password (12+ characters)";
      password.setAttribute("aria-label", `New password for ${user.username}`);
      const button = document.createElement("button");
      button.textContent = "Reset password";
      button.className = "secondary";
      form.append(password, button);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        button.disabled = true;
        try {
          await api(`/api/users/${encodeURIComponent(user.username)}/password`, {
            method: "PUT", body: JSON.stringify({password: password.value}),
          });
          form.reset();
          showDialogNotice(panelNode("account-notice"), `Password reset for ${user.username}. Their previous sessions are now signed out.`, "success");
        } catch (error) { showDialogNotice(panelNode("account-notice"), error.message, "error"); }
        finally { button.disabled = false; }
      });
      row.append(form);
    }
    panelNode("users-list").append(row);
  }
}

async function refreshManagerUpdate() {
  try {
    const update = await api("/api/manager/update");
    panelNode("manager-update-status").textContent = update.enabled
      ? `Update status: ${update.state}${update.message ? ` — ${update.message}` : ""}`
      : "The update service needs to be installed on this controller. Run the existing command-line manager updater once to install it.";
    panelNode("manager-update").disabled = !update.enabled || ["queued", "running"].includes(update.state);
  } catch (error) {
    panelNode("manager-update-status").textContent = `Waiting for controller: ${error.message}`;
  }
}

panelNode("account-settings").addEventListener("click", async () => {
  try {
    accountSession = await api("/api/session");
    if (!accountSession.authenticated) { showLogin(); return; }
    panelNode("account-identity").textContent = `Signed in as ${accountSession.username}`;
    panelNode("account-notice").hidden = true;
    panelNode("admin-settings").hidden = !accountSession.admin;
    panelNode("account-dialog").showModal();
    if (accountSession.admin) {
      await refreshUsers();
      await refreshManagerUpdate();
      clearInterval(updatePoll);
      updatePoll = setInterval(refreshManagerUpdate, 5000);
    }
  } catch (error) { showDialogNotice(panelNode("account-notice"), error.message, "error"); }
});
panelNode("account-dialog").addEventListener("close", () => {
  clearInterval(updatePoll);
  panelNode("account-dialog").querySelectorAll('input[type="password"]').forEach((input) => { input.value = ""; });
});
panelNode("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  event.submitter.disabled = true;
  try {
    await api(`/api/users/${encodeURIComponent(accountSession.username)}/password`, {
      method: "PUT", body: JSON.stringify({
        password: panelNode("new-password").value, current_password: panelNode("current-password").value,
      }),
    });
    event.target.reset();
    showDialogNotice(panelNode("account-notice"), "Password changed. Other sessions have been signed out.", "success");
  } catch (error) { showDialogNotice(panelNode("account-notice"), error.message, "error"); }
  finally { event.submitter.disabled = false; }
});
panelNode("add-user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  event.submitter.disabled = true;
  try {
    await api("/api/users", {method: "POST", body: JSON.stringify({
      username: panelNode("add-username").value, password: panelNode("add-password").value,
      admin: panelNode("add-admin").checked,
    })});
    event.target.reset();
    await refreshUsers();
    showDialogNotice(panelNode("account-notice"), "User added.", "success");
  } catch (error) { showDialogNotice(panelNode("account-notice"), error.message, "error"); }
  finally { event.submitter.disabled = false; }
});
panelNode("manager-update").addEventListener("click", async () => {
  if (!window.confirm("Update the manager from its configured branch? The control panel will briefly disconnect.")) return;
  panelNode("manager-update").disabled = true;
  try {
    await api("/api/manager/update", {method: "POST"});
    panelNode("manager-update-status").textContent = "Update queued. Waiting for controller…";
  } catch (error) {
    showDialogNotice(panelNode("account-notice"), error.message, "error");
    await refreshManagerUpdate();
  }
});
