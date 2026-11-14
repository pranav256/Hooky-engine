// Named projects use the authenticated Studio API; edits reach disk only on Save.
let currentProject = null, projectBaseline = "", projectLoading = false;
let savedProjects = [], projectListFailed = false, projectMessage = "";

function projectPayload() {
  return { name: document.getElementById("project-name").value.trim(), studio: {
    url: document.getElementById("source-url").value.trim(),
    topic: document.getElementById("clip-topic").value.trim(),
    video_title: currentSourceVideoTitle, hooks: CURRENT_HOOKS.map((hook) => ({
      id: String(hook.id), start: hook.start, end: hook.end, title: hook.title || "", category: hook.category || "",
      hook_quote: hook.hook_quote || "", reason: hook.reason || "", cues: hook.cues || [], caption_edits: hook.caption_edits || [],
      ...(hook.retention_score != null ? { retention_score: hook.retention_score } : {}),
    })),
    selected_hook: String(selectedHookId), framing: activeFramingMode, subtitle: activeSubtitleMode,
    recommended_framing: autoDetectedFraming, recommended_style: autoDetectedStyle, notice: analysisNotice,
  } };
}
function projectDirty() {
  return CURRENT_HOOKS.length > 0 && (!currentProject || JSON.stringify(projectPayload()) !== projectBaseline);
}
function updateProjectControls() {
  const busy = projectLoading || jobStarting || Boolean(activeJobId) || jobsLoading;
  const canSave = CURRENT_HOOKS.length && sourceForHooks === document.getElementById("source-url").value.trim();
  document.getElementById("project-save").disabled = busy || !canSave;
  document.getElementById("project-copy").disabled = busy || !canSave || !currentProject;
  document.getElementById("project-copy").hidden = !currentProject;
  document.getElementById("project-open").disabled = busy || projectListFailed || !document.getElementById("project-list").value;
  document.getElementById("project-refresh").disabled = busy;
  document.getElementById("project-refresh").hidden = !projectListFailed && !projectMessage.startsWith("Could not load projects") && !projectMessage.includes("could not be read");
  document.getElementById("project-list").disabled = busy || projectListFailed || !savedProjects.length;
  document.getElementById("project-name").disabled = busy;
  document.getElementById("project-save").textContent = projectLoading ? "Please wait…" : "Save project";
  document.getElementById("project-status").textContent = projectLoading ? "Loading or saving projects…" : projectMessage ||
    (currentProject ? projectDirty() ? `Unsaved changes to ${currentProject.name}. Choose Save project.` : `Saved project: ${currentProject.name}.`
      : CURRENT_HOOKS.length ? "Name this project, then choose Save project." : "Detect hooks to save your first project, or open a saved project.");
}
function renderProjectList() {
  const select = document.getElementById("project-list"), previous = select.value;
  select.replaceChildren(new Option(savedProjects.length ? "Choose a saved project" : "No saved projects yet", ""));
  savedProjects.forEach((project) => select.add(new Option(project.name, project.id)));
  select.value = savedProjects.some((project) => project.id === previous) ? previous : currentProject?.id || "";
  updateProjectControls();
}
function detachProject() {
  currentProject = null;
  projectBaseline = "";
  projectMessage = "";
  document.getElementById("project-name").value = "";
}
function confirmProjectChange(message) {
  return !projectDirty() || window.confirm(message + " Unsaved project changes will be discarded. Cancel to save them first.");
}
function applyProject(project, restoreEdits = false) {
  const pending = restoreEdits ? { source: document.getElementById("source-url").value,
    topic: document.getElementById("clip-topic").value, name: document.getElementById("project-name").value,
    framing: activeFramingMode, subtitle: activeSubtitleMode, selected: selectedHookId,
    edits: window._restoredTiming, revision: currentProject?.revision } : null;
  currentProject = { id: project.id, name: project.name, revision: project.revision };
  const job = { id: "project_" + project.id, url: project.studio.url, result: project.studio };
  document.getElementById("project-name").value = project.name;
  selectedHookId = project.studio.selected_hook;
  setFramingMode(project.studio.framing);
  setSubtitleMode(project.studio.subtitle);
  hydrateAnalysis(job);
  projectBaseline = JSON.stringify(projectPayload());
  if (pending) {
    window._restoredTiming = pending.edits;
    selectedHookId = pending.selected;
    hydrateAnalysis(job, true);
    document.getElementById("source-url").value = pending.source || project.studio.url;
    document.getElementById("clip-topic").value = pending.topic;
    document.getElementById("project-name").value = pending.name;
    setFramingMode(pending.framing);
    setSubtitleMode(pending.subtitle);
    if (pending.revision !== project.revision) {
      currentProject.revision = pending.revision;
      projectMessage = "This project changed in another tab. Your browser edits were restored. Save as new to keep both versions.";
    }
  }
  document.getElementById("caption-search").value = "";
  document.getElementById("timing-error").textContent = "";
  renderHooksList();
  updateSelectedHookDetails();
  persistStudio();
}
async function loadProjects(restore = false) {
  if (projectLoading) return;
  projectLoading = true;
  projectMessage = "";
  updateJobControls();
  let listed = false;
  try {
    const data = await apiJson("/api/projects");
    if (!Array.isArray(data.projects)) throw new Error("Invalid project list response.");
    savedProjects = data.projects;
    projectListFailed = false;
    listed = true;
    renderProjectList();
    if ((restore || !CURRENT_HOOKS.length) && currentProject) {
      const project = await apiJson("/api/projects/" + encodeURIComponent(currentProject.id));
      applyProject(project, true);
    }
    if (data.unreadable_count) projectMessage = `${data.unreadable_count} saved project files could not be read. The files were kept.`;
    renderProjectList();
  } catch (error) {
    projectListFailed = !listed;
    projectMessage = "Could not load projects. " + error.message + " Choose Refresh list to retry.";
  } finally {
    projectLoading = false;
    updateJobControls();
  }
}
async function saveProject(asNew = false) {
  if (projectLoading || jobStarting || activeJobId || jobsLoading) return;
  const name = document.getElementById("project-name");
  if (!name.value.trim()) {
    projectMessage = "Enter a project name before saving.";
    name.setAttribute("aria-invalid", "true");
    updateProjectControls();
    name.focus();
    return;
  }
  if (!CURRENT_HOOKS.length || sourceForHooks !== document.getElementById("source-url").value.trim()) return;
  if (!applyTiming()) {
    document.getElementById(document.getElementById("timing-error").dataset.field)?.focus();
    return;
  }
  const payload = JSON.parse(JSON.stringify(projectPayload())), id = asNew ? null : currentProject?.id;
  if (asNew && payload.name === currentProject?.name) payload.name = payload.name.slice(0, 95) + " copy";
  if (id) payload.revision = currentProject.revision;
  projectLoading = true;
  projectMessage = "";
  name.removeAttribute("aria-invalid");
  updateJobControls();
  try {
    const project = await apiJson("/api/projects" + (id ? "/" + encodeURIComponent(id) : ""), {
      method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    currentProject = { id: project.id, name: project.name, revision: project.revision };
    name.value = project.name;
    delete payload.revision;
    projectBaseline = JSON.stringify(payload);
    savedProjects = [{ id: project.id, name: project.name, revision: project.revision }, ...savedProjects.filter((item) => item.id !== project.id)];
    document.getElementById("project-list").value = "";
    projectListFailed = false;
    renderProjectList();
    persistStudio();
  } catch (error) {
    projectMessage = "Could not save project. " + error.message + " Your edits are still here.";
  } finally {
    projectLoading = false;
    updateJobControls();
  }
}
async function openProject() {
  const id = document.getElementById("project-list").value;
  if (!id || projectLoading || jobStarting || activeJobId || jobsLoading) return;
  if (!confirmProjectChange("Open the selected saved project?")) return;
  projectLoading = true;
  projectMessage = "";
  updateJobControls();
  try {
    const project = await apiJson("/api/projects/" + encodeURIComponent(id));
    applyProject(project);
    switchTab("studio");
    document.getElementById("studio-alert").classList.add("hidden");
    document.getElementById("project-name").focus();
  } catch (error) {
    projectMessage = "Could not open project. " + error.message + " Your current edits were kept.";
  } finally {
    projectLoading = false;
    updateJobControls();
    document.getElementById("project-name").focus();
  }
}
