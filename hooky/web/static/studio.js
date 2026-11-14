// Security & Auth Token Integration

const _injectedToken =
  !window.__HOOKY_TOKEN__.includes("PLACEHOLDER") &&
  window.__HOOKY_TOKEN__.length > 10
    ? window.__HOOKY_TOKEN__
    : null;
const _urlParams = new URLSearchParams(window.location.search);
const _queryToken = _urlParams.get("token");

if (_injectedToken) {
  window.__HOOKY_TOKEN__ = _injectedToken;
  try {
    sessionStorage.setItem("hooky_token", _injectedToken);
  } catch (e) {
    /* Storage and URL cleanup are optional; retain the in-memory session. */
  }
  if (_queryToken) {
    const cleanUrl = window.location.pathname + (window.location.hash || "");
    try {
      window.history.replaceState({}, document.title, cleanUrl);
    } catch (e) {
      /* Storage and URL cleanup are optional; retain the in-memory session. */
    }
  }
} else if (_queryToken) {
  window.__HOOKY_TOKEN__ = _queryToken;
  try {
    sessionStorage.setItem("hooky_token", _queryToken);
  } catch (e) {
    /* Storage and URL cleanup are optional; retain the in-memory session. */
  }
} else {
  try {
    const _saved = sessionStorage.getItem("hooky_token");
    if (_saved) {
      window.__HOOKY_TOKEN__ = _saved;
    }
  } catch (e) {
    /* Storage and URL cleanup are optional; retain the in-memory session. */
  }
}

// Intercept fetch to automatically include X-Hooky-Token header on all API calls
const _nativeFetch = window.fetch;
window.fetch = function (input, init) {
  init = init || {};
  init.credentials = init.credentials || "same-origin";
  init.headers = init.headers || {};
  const token = window.__HOOKY_TOKEN__;
  const target = new URL(
    typeof input === "string" ? input : input.url || input,
    window.location.href,
  );
  if (
    target.origin === window.location.origin &&
    target.pathname.startsWith("/api/") &&
    token &&
    !token.includes("PLACEHOLDER")
  ) {
    if (init.headers instanceof Headers) {
      init.headers.set("X-Hooky-Token", token);
    } else if (Array.isArray(init.headers)) {
      init.headers.push(["X-Hooky-Token", token]);
    } else {
      init.headers["X-Hooky-Token"] = token;
    }
  }
  return _nativeFetch(input, init);
};

// State management (starts clean with no preloaded video or fake hooks)
let CURRENT_HOOKS = [];
let currentSourceVideoTitle = "";
let currentAnalysisTopic = "", analysisNotice = "";
let selectedHookId = null;
let activeFramingMode = "auto";
let activeSubtitleMode = "auto";
let autoDetectedFraming = "fit";
let autoDetectedStyle = "gold";
let currentPreviewMode = "player";
let cachedChannels = [];
let savedChannels = "[]";
let channelsLoaded = false;
let channelsSaving = false;
let cachedEnvConfig = { raw: "", values: {} };
let isPlaying = false;
let wordIdx = 0;
let SCRIPT_WORDS = [];
let captionTimer = null;
let envFormBaseline = {};
let envDraftOperations = [],
  envEditorDirty = false;

const TERMINAL_JOBS = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);
let jobs = [],
  activeJobId = null,
  selectedAnalysisId = null,
  sourceForHooks = "",
  activeTab = "studio";
let jobStarting = false,
  jobLoopRunning = false,
  jobsLoading = false,
  jobsFailed = false,
  cancelPending = false;
let pageLeaving = false,
  pollController = null;
const jobLogOffsets = new Map();
let envLoaded = false,
  envLoading = false,
  envSaving = false,
  envDirty = false,
  envTab = "form",
  envSwitching = false,
  envRevision = 0;
let outputsLoading = false,
  doctorReady = false,
  onboardingDismissed = false;
let outputPlayerTrigger = null;
const ENV_FIELDS = {
  GEMINI_API_KEY: "env-gemini-key",
  GEMINI_MODEL: "env-gemini-model",
  SUB_STYLE: "env-sub-style",
  GEMINI_MODEL_FALLBACKS: "env-gemini-fallbacks",
  AUTO_CLIP_DENSITY_MINUTES: "env-density-minutes",
  FFMPEG_THREADS: "env-ffmpeg-threads",
};
async function apiJson(url, init = {}) {
  const response = await fetch(url, {
    ...init,
    signal: init.signal || AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Request failed (HTTP " + response.status + ")",
    );
    error.status = response.status;
    throw error;
  }
  return data;
}
function persistStudio() {
  const source = document.getElementById("source-url").value.trim();
  let sourceSafe = source;
  try {
    const url = new URL(source);
    if (
      url.username ||
      url.password ||
      [...url.searchParams.keys()].some(
        (key) => !["v", "t", "start"].includes(key),
      )
    )
      sourceSafe = "";
  } catch {
    sourceSafe = "";
  }
  const edits = CURRENT_HOOKS.slice(0, 100).map((hook) => ({
    id: hook.id,
    start: hook.start,
    end: hook.end,
    caption_edits: hook.caption_edits || [],
  }));
  if (CURRENT_HOOKS.length) window._restoredTiming = edits;
  try {
    localStorage.setItem(
      "hooky_studio_v1",
      JSON.stringify({
        source: sourceSafe,
        topic: document.getElementById("clip-topic").value,
        selectedAnalysisId,
        edits,
        selected: selectedHookId,
        framing: activeFramingMode,
        subtitle: activeSubtitleMode,
        activeJobId,
        activeTab,
        onboardingDismissed,
      }),
    );
    return true;
  } catch {
    /* Storage and URL cleanup are optional; retain the in-memory session. */
    return false;
  }
}
function restoreStudio() {
  try {
    const saved = JSON.parse(localStorage.getItem("hooky_studio_v1") || "null");
    if (!saved) return;
    document.getElementById("source-url").value =
      typeof saved.source === "string" ? saved.source : "";
    document.getElementById("clip-topic").value = typeof saved.topic === "string" ? saved.topic.slice(0, 500) : "";
    selectedAnalysisId = saved.selectedAnalysisId || null;
    selectedHookId = saved.selected;
    activeJobId = saved.activeJobId || null;
    activeTab = saved.activeTab || "studio";
    onboardingDismissed = Boolean(saved.onboardingDismissed);
    window._restoredTiming = Array.isArray(saved.edits)
      ? saved.edits.slice(0, 100)
      : [];
    activeFramingMode = ["auto", "fit", "center"].includes(saved.framing)
      ? saved.framing
      : "auto";
    activeSubtitleMode = [
      "auto",
      "gold",
      "hormozi",
      "clean",
      "minimal",
      "emerald",
      "crimson",
    ].includes(saved.subtitle)
      ? saved.subtitle
      : "auto";
  } catch {
    /* Storage and URL cleanup are optional; retain the in-memory session. */
  }
}
function updateOnboarding() {
  const hasKey = Boolean(cachedEnvConfig.values?.GEMINI_API_KEY);
  const exported =
    Number(document.getElementById("gallery-count").textContent) > 0;
  document.getElementById("first-run").hidden = onboardingDismissed || exported;
  document.getElementById("setup-health").hidden = doctorReady;
  document.getElementById("setup-key").hidden = !doctorReady || hasKey;
  document.getElementById("setup-analysis").hidden =
    !doctorReady || !hasKey || CURRENT_HOOKS.length > 0;
  document.getElementById("setup-export").hidden = !CURRENT_HOOKS.length;
  document.getElementById("setup-health").textContent = doctorReady
    ? "System health checked"
    : "Check system health";
  document.getElementById("setup-key").textContent = cachedEnvConfig.values
    ?.GEMINI_API_KEY
    ? "API key configured"
    : "Configure API key";
  document.getElementById("setup-analysis").textContent = CURRENT_HOOKS.length
    ? "Choose a moment to export"
    : "Analyze your first source";
}
function dismissOnboarding() {
  onboardingDismissed = true;
  updateOnboarding();
  persistStudio();
}

document.addEventListener("DOMContentLoaded", () => {
  lucide.createIcons();
  document.querySelectorAll("label").forEach((label) => {
    const input = label.parentElement.querySelector("input,select,textarea");
    if (!label.htmlFor && input?.id) label.htmlFor = input.id;
  });
  document.getElementById("source-url").addEventListener("keydown", (event) => {
    if (event.key === "Enter") triggerAnalyze();
  });
  document
    .getElementById("new-channel-input")
    .addEventListener("keydown", (event) => {
      if (event.key === "Enter") addNewChannel();
    });
  document.querySelectorAll("button").forEach((button) => {
    if (!button.textContent.trim() && !button.getAttribute("aria-label"))
      button.setAttribute(
        "aria-label",
        button
          .querySelector("[data-lucide]")
          ?.getAttribute("data-lucide")
          ?.replaceAll("-", " ") || "Studio action",
      );
  });
  restoreStudio();
  document.getElementById("source-url").addEventListener("input", () => {
    persistStudio();
    updateJobControls();
  });
  document.getElementById("clip-topic").addEventListener("input", persistStudio);
  document.getElementById("clip-topic").addEventListener("keydown", (event) => {
    if (event.key === "Enter") triggerAnalyze();
  });
  document
    .querySelectorAll(
      "#env-form-view input, #env-form-view select, #env-raw-textarea",
    )
    .forEach((field) =>
      field.addEventListener("input", () => {
        envDirty = true;
        envEditorDirty = true;
        envRevision++;
        setEnvStatus(
          "Unsaved settings. Save configuration to apply them to future jobs.",
          "unsaved",
        );
      }),
    );
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeActivePlayer();
    if (
      (event.ctrlKey || event.metaKey) &&
      event.key === "Enter" &&
      event.target.id === "env-raw-textarea"
    ) {
      event.preventDefault();
      saveEnvForm();
    }
  });
  switchTab(activeTab);
  setFramingMode(activeFramingMode);
  setSubtitleMode(activeSubtitleMode);
  switchPreviewMode("player");
  renderHooksList();
  updateSelectedHookDetails();
  loadDoctorReport();
  loadChannels();
  loadOutputs();
  loadEnvConfig();
  loadJobs();
  updateOnboarding();
});

function switchTab(tabId) {
  clearTimeout(captionTimer);
  activeTab = ["studio", "radar", "gallery", "settings", "doctor"].includes(
    tabId,
  )
    ? tabId
    : "studio";
  tabId = activeTab;
  if (document.readyState !== "loading") persistStudio();
  const names = {
    studio: "Clip studio",
    radar: "Channel radar",
    gallery: "Your clips",
    settings: "Settings",
    doctor: "System health",
  };
  document.getElementById("workspace-label").textContent =
    names[tabId] || "Clip studio";
  ["studio", "radar", "gallery", "settings", "doctor"].forEach((t) => {
    document
      .getElementById(`view-${t}`)
      .classList.toggle("hidden", t !== tabId);
    const btn = document.getElementById(`tab-${t}`);
    if (btn) {
      if (t === tabId) btn.setAttribute("aria-current", "page");
      else btn.removeAttribute("aria-current");
      btn.className = "tab-btn";
    }
  });
  lucide.createIcons();
  scheduleCaptionPreview();
}

function setFramingMode(mode) {
  activeFramingMode = mode;
  ["auto", "fit", "center"].forEach((m) => {
    const btn = document.getElementById(`frame-mode-${m}`);
    btn.setAttribute("aria-pressed", String(m === mode));
    if (m === mode) {
      btn.className =
        "px-2.5 py-0.5 bg-[var(--hooky-raised)] text-white font-medium rounded transition-all";
    } else {
      btn.className =
        "px-2.5 py-0.5 text-slate-400 hover:text-white rounded transition-all";
    }
  });
  applyVisualFramingState();
  if (document.readyState !== "loading") persistStudio();
}

function setSubtitleMode(style) {
  activeSubtitleMode = style;
  ["auto", "gold", "hormozi", "clean", "minimal", "emerald", "crimson"].forEach(
    (s) => {
      const btn = document.getElementById(`sub-mode-${s}`);
      if (btn) {
        btn.setAttribute("aria-pressed", String(s === style));
        if (s === style) {
          btn.className =
            "px-2.5 py-0.5 bg-[var(--hooky-raised)] text-white font-medium rounded transition-all";
        } else {
          btn.className =
            "px-2.5 py-0.5 text-slate-400 hover:text-white rounded transition-all";
        }
      }
    },
  );
  renderSubtitles();
  if (document.readyState !== "loading") persistStudio();
}

function applyVisualFramingState() {
  const effectiveMode =
    activeFramingMode === "auto" ? autoDetectedFraming : activeFramingMode;
  const cropBox = document.getElementById("crop-box");
  const tag = document.getElementById("preview-mode-tag");

  if (effectiveMode === "fit") {
    cropBox.style.width = "40%";
    cropBox.style.left = "30%";
    tag.innerText = "Canvas fit (ambient blur)";
  } else {
    cropBox.style.width = "46%";
    cropBox.style.left = "27%";
    tag.innerText = "Center crop (solo speaker)";
  }
}

function switchPreviewMode(mode) {
  clearTimeout(captionTimer);
  currentPreviewMode = mode;
  const inactive = document.getElementById(
    mode === "player" ? "canvas-video-frame" : "youtube-embed-frame",
  );
  inactive.contentWindow?.postMessage(
    JSON.stringify({ event: "command", func: "pauseVideo", args: [] }),
    "https://www.youtube-nocookie.com",
  );
  scheduleCaptionPreview();
  const isPlayer = mode === "player";
  document.getElementById("preview-play-btn").hidden = isPlayer;
  document.getElementById("preview-play-btn").textContent = matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches
    ? "Next caption"
    : isPlaying
      ? "Pause captions"
      : "Play captions";
  document
    .getElementById("preview-tab-player")
    .setAttribute("aria-pressed", String(isPlayer));
  document
    .getElementById("preview-tab-canvas")
    .setAttribute("aria-pressed", String(!isPlayer));
  document
    .getElementById("preview-player-container")
    .classList.toggle("hidden", !isPlayer);
  document
    .getElementById("preview-canvas-container")
    .classList.toggle("hidden", isPlayer);

  const tabPlayer = document.getElementById("preview-tab-player");
  const tabCanvas = document.getElementById("preview-tab-canvas");

  if (isPlayer) {
    tabPlayer.className =
      "px-3 py-1 bg-[var(--hooky-raised)] text-white font-medium rounded border border-white/10 transition-all";
    tabCanvas.className =
      "px-3 py-1 text-slate-400 hover:text-white rounded transition-all";
  } else {
    tabCanvas.className =
      "px-3 py-1 bg-[var(--hooky-raised)] text-white font-medium rounded border border-white/10 transition-all";
    tabPlayer.className =
      "px-3 py-1 text-slate-400 hover:text-white rounded transition-all";
  }
}

function extractYouTubeId(url) {
  if (!url) return null;
  const m = url.match(
    /(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|embed\/|v\/|shorts\/))([\w-]{11})/i,
  );
  return m ? m[1] : null;
}

function selectHook(hookId) {
  selectedHookId = String(hookId);
  wordIdx = 0;
  persistStudio();
  renderHooksList();
  updateSelectedHookDetails();
}

function updateSelectedHookDetails() {
  const hook =
    CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId)) ||
    (CURRENT_HOOKS.length > 0 ? CURRENT_HOOKS[0] : null);
  const iframe = document.getElementById("youtube-embed-frame");
  const placeholder = document.getElementById("player-placeholder");
  const timeBadge = document.getElementById("transcript-time-badge");
  const dialogueBox = document.getElementById("dialogue-lines-box");
  const previewElem = document.getElementById("canvas-subtitle-preview");

  const videoId = extractYouTubeId(sourceForHooks);
  const embedUrl = videoId
    ? "https://www.youtube-nocookie.com/embed/" +
      videoId +
      "?enablejsapi=1" +
      (hook
        ? "&start=" + Math.floor(hook.start) + "&end=" + Math.floor(hook.end)
        : "")
    : "";
  for (const player of [
    iframe,
    document.getElementById("canvas-video-frame"),
  ]) {
    if (embedUrl) {
      if (player.getAttribute("src") !== embedUrl) player.src = embedUrl;
    } else player.removeAttribute("src"); // Empty src can reload Studio inside the frame.
    player.classList.toggle("hidden", !embedUrl);
  }
  if (placeholder) {
    placeholder.classList.toggle("hidden", Boolean(embedUrl));
    placeholder.querySelector("p").textContent = hook
      ? "Source playback is unavailable here. Preview captions in the 9:16 canvas, then export to watch your clip."
      : "Enter a source URL, then detect hooks or run the pipeline.";
  }
  if (!hook) {
    if (timeBadge) timeBadge.innerText = "--:-- - --:--";
    if (dialogueBox)
      dialogueBox.innerHTML =
        '<p class="text-slate-500 text-xs italic">Select or detect a hook to view transcript cues.</p>';
    if (previewElem)
      previewElem.innerHTML =
        '<span class="text-slate-500">Subtitle preview will appear here</span>';
    renderCaptionEditor();
    return;
  }

  document.getElementById("timing-start").value = hook.start;
  document.getElementById("timing-end").value = hook.end;
  if (timeBadge) {
    timeBadge.innerText = `${hook.start_fmt} - ${hook.end_fmt} (${hook.duration})`;
  }
  renderCaptionEditor();
  renderSubtitles();
}

function captionText(hook, cue) {
  return hook.caption_edits?.find((edit) => edit.start === cue.start && edit.end === cue.end)?.text ?? cue.text;
}
function renderCaptionEditor() {
  const hook = CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId)),
    box = document.getElementById("dialogue-lines-box"),
    status = document.getElementById("caption-edit-status"),
    search = document.getElementById("caption-search"),
    reset = document.getElementById("caption-reset");
  const cues = Array.isArray(hook?.cues) ? hook.cues : [];
  search.disabled = !cues.length;
  reset.disabled = !hook?.caption_edits?.length;
  if (!cues.length) {
    box.innerHTML = hook?.dialogue?.map((line) => `<p>${escapeHtml(line.time)} · ${escapeHtml(line.text)}</p>`).join("") || "Select a moment to edit its captions.";
    status.textContent = hook ? "Analyze this source again to enable caption editing." : "";
    return;
  }
  const query = search.value.trim().toLowerCase(),
    visible = cues.map((cue, index) => ({ cue, index })).filter(({ cue }) =>
      cue.end > hook.start && cue.start < hook.end && captionText(hook, cue).toLowerCase().includes(query)),
    total = cues.filter((cue) => cue.end > hook.start && cue.start < hook.end).length;
  box.innerHTML = visible.map(({ cue, index }) => `
    <div class="caption-phrase">
      <label for="caption-${index}">Caption at ${escapeHtml(formatSecondsToMMSS(cue.start))} (${cue.start.toFixed(3)}s)</label>
      <textarea id="caption-${index}" rows="2" maxlength="1000" aria-invalid="${!captionText(hook, cue).trim()}" aria-describedby="caption-edit-status" oninput="editCaption(${index}, this)">${escapeHtml(captionText(hook, cue))}</textarea>
    </div>`).join("") || "No captions match your search. Clear the search to show this clip's captions.";
  status.textContent = hook.caption_edits?.some((edit) => !edit.text.trim())
    ? "Enter caption text before exporting, or reset the captions."
    : `${visible.length} of ${total} captions shown. ${hook.caption_edits?.length ? "Edits restored." : ""}`;
}
function editCaption(index, field) {
  const hook = CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId)), cue = hook?.cues?.[index];
  if (!cue) return;
  hook.caption_edits = (hook.caption_edits || []).filter((edit) => edit.start !== cue.start || edit.end !== cue.end);
  if (field.value !== cue.text) hook.caption_edits.push({ start: cue.start, end: cue.end, text: field.value });
  field.setAttribute("aria-invalid", String(!field.value.trim()));
  const saved = persistStudio();
  document.getElementById("caption-edit-status").textContent = !field.value.trim()
    ? "Enter caption text before exporting, or reset the captions."
    : saved ? "Caption changes saved in this browser. Export uses these edits." : "Caption changes kept for this session. Browser storage is unavailable.";
  document.getElementById("caption-reset").disabled = !hook.caption_edits.length;
  const alert = document.getElementById("studio-alert");
  if (field.value.trim() && alert.textContent.startsWith("Correct empty or oversized caption")) alert.classList.add("hidden");
  wordIdx = 0;
  renderSubtitles();
}
function resetCaptions() {
  const hook = CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId));
  if (!hook) return;
  hook.caption_edits = [];
  persistStudio();
  renderCaptionEditor();
  renderSubtitles();
  document.getElementById("caption-edit-status").textContent = "Original captions restored.";
  const alert = document.getElementById("studio-alert");
  if (alert.textContent.startsWith("Correct empty or oversized caption")) alert.classList.add("hidden");
  document.getElementById("caption-search").focus();
}

function renderSubtitles() {
  const hook =
    CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId)) ||
    (CURRENT_HOOKS.length > 0 ? CURRENT_HOOKS[0] : null);
  const previewElem = document.getElementById("canvas-subtitle-preview");
  if (!previewElem) return;
  if (!hook) {
    previewElem.innerHTML =
      '<span class="text-slate-500">Subtitle preview will appear here</span>';
    return;
  }

  const style =
    activeSubtitleMode === "auto" ? autoDetectedStyle : activeSubtitleMode;
  const editedWords = (hook.cues || []).filter((cue) => cue.end > hook.start && cue.start < hook.end)
    .flatMap((cue) => captionText(hook, cue).trim().split(/\s+/).filter(Boolean));
  const words = editedWords.length ? editedWords :
    hook.words && hook.words.length > 0
      ? hook.words
      : hook.title
        ? hook.title.toUpperCase().split(/\s+/)
        : ["HOOKY", "CLIP"];
  const rawWord = words[wordIdx % words.length];
  const activeWord = escapeHtml(
    typeof rawWord === "object" ? rawWord.word || rawWord.text || "" : rawWord,
  );

  let styledHTML = "";
  if (style === "gold") {
    styledHTML = `<span class="text-white">${escapeHtml(hook.title || "")}</span> <span class="text-[#ffd166] font-medium text-sm">${activeWord}</span>`;
  } else if (style === "hormozi") {
    styledHTML = `<span class="bg-[#ffff00] text-black px-1.5 py-0.5 font-bold uppercase text-sm">${activeWord}</span>`;
  } else if (style === "emerald") {
    styledHTML = `<span class="text-white">${escapeHtml(hook.title || "")}</span> <span class="text-[var(--hooky-accent)] font-medium underline decoration-1">${activeWord}</span>`;
  } else if (style === "crimson") {
    styledHTML = `<span class="text-[#ef4444] font-medium text-sm">${activeWord}</span>`;
  } else if (style === "minimal") {
    styledHTML = `<span class="text-slate-100 border-b border-white">${activeWord}</span>`;
  } else {
    styledHTML = `<span class="text-white bg-black/80 px-2 py-0.5 border border-white/20">${activeWord}</span>`;
  }
  previewElem.innerHTML = styledHTML;
}

function renderHooksList() {
  updateJobControls();
  const focusedHookId = document.activeElement?.dataset.hookId;
  const focusedExport =
    document.activeElement?.hasAttribute("data-export-hook");
  const container = document.getElementById("hooks-container");
  if (!container) return;

  document.getElementById("hook-count").innerText =
    `${CURRENT_HOOKS.length} hooks detected`;
  const summary = document.getElementById("topic-results-summary");
  summary.hidden = !currentAnalysisTopic;
  summary.textContent = currentAnalysisTopic ? `Topic: ${currentAnalysisTopic}. ${CURRENT_HOOKS.length} matches. ${analysisNotice}` : "";

  if (CURRENT_HOOKS.length === 0) {
    if (selectedAnalysisId && currentAnalysisTopic) {
      container.innerHTML = '<div class="brutal-card p-5"><p>No matching spoken moments found.</p><p>Try a broader topic or clear it to detect general hooks.</p></div>';
      return;
    }
    container.innerHTML = `
          <div class="studio-empty p-8 border border-white/[0.08] bg-[var(--hooky-panel)] rounded-lg text-center space-y-3">
            <i data-lucide="video" class="w-8 h-8 text-slate-500 mx-auto"></i>
            <div>
              <p class="text-sm font-medium text-slate-300">The next good moment starts here.</p>
              <p class="text-xs text-slate-500 mt-1 font-mono">Paste a video link and choose “Detect hooks”. Your AI-selected moments will appear here.</p>
            </div>
          </div>
        `;
    lucide.createIcons();
    return;
  }

  container.innerHTML = CURRENT_HOOKS.map((h, idx) => {
    const isSelected = String(h.id) === String(selectedHookId);
    const borderClass = isSelected
      ? "border-[var(--hooky-accent)] bg-[var(--hooky-raised)]"
      : "border-white/[0.08] bg-[var(--hooky-panel)] hover:border-white/20";

    return `
          <div
            class="hook-card p-4 border ${borderClass} rounded-lg transition-all cursor-pointer space-y-2.5 group"
          >
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-5 h-5 grid place-items-center bg-[var(--hooky-bg)] text-slate-400 font-mono text-[10px] rounded border border-white/10">0${idx + 1}</span>
                <span class="text-[11px] font-mono px-2 py-0.5 bg-white/5 text-slate-300 border border-white/10 rounded">
                  ${escapeHtml(h.category || "AI-selected")}
                </span>
              </div>
              <div class="flex items-center gap-1 font-mono text-xs tabular">
                <span class="text-[var(--hooky-accent)] font-medium">${h.retention_score == null ? "Topic match" : escapeHtml(h.retention_score) + "%"}</span>
                <span class="text-slate-500 text-[10px]">${h.retention_score == null ? "" : "Hook score"}</span>
              </div>
            </div>

            <div>
              <h4 class="font-medium text-sm text-white group-hover:text-white transition-colors leading-snug">
                <button type="button" class="hook-select" data-hook-id="${escapeHtml(h.id)}" onclick="selectHook(this.dataset.hookId)" aria-pressed="${isSelected}">${escapeHtml(h.title)}</button>
              </h4>
              <p class="text-xs text-slate-400 mt-1 line-clamp-2 font-mono leading-relaxed">
                "${escapeHtml(h.hook_quote)}"
              </p>
              ${currentAnalysisTopic ? `<p class="topic-summary">${escapeHtml(h.reason || "")}</p>` : ""}
            </div>

            <div class="flex items-center justify-between pt-2 border-t border-white/[0.06] text-[11px] font-mono">
              <span class="text-slate-400 tabular">
                ${escapeHtml(h.start_fmt)} - ${escapeHtml(h.end_fmt)} (${escapeHtml(h.duration)})
              </span>
              <button
                data-export-hook="true" ${activeJobId || jobStarting || jobsLoading || jobsFailed || sourceForHooks !== document.getElementById("source-url").value.trim() ? "disabled" : ""} data-hook-id="${escapeHtml(h.id)}" onclick="event.stopPropagation(); renderSingleHookById(this.dataset.hookId)"
                class="px-2.5 py-1 bg-[var(--hooky-raised)] hover:bg-white hover:text-black border border-white/10 text-white rounded transition-all"
              >
                Export reel →
              </button>
            </div>
          </div>
        `;
  }).join("");
  if (focusedHookId)
    [...container.querySelectorAll("[data-hook-id]")]
      .find(
        (button) =>
          button.dataset.hookId === focusedHookId &&
          button.hasAttribute("data-export-hook") === focusedExport,
      )
      ?.focus({ preventScroll: true });
  lucide.createIcons();
}

function scheduleCaptionPreview() {
  clearTimeout(captionTimer);
  if (
    !isPlaying ||
    document.hidden ||
    activeTab !== "studio" ||
    currentPreviewMode !== "canvas" ||
    !CURRENT_HOOKS.length ||
    matchMedia("(prefers-reduced-motion: reduce)").matches
  )
    return;
  captionTimer = setTimeout(() => {
    wordIdx++;
    renderSubtitles();
    scheduleCaptionPreview();
  }, 380);
}
document.addEventListener("visibilitychange", scheduleCaptionPreview);
function togglePreviewPlayback() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    wordIdx++;
    renderSubtitles();
    document.getElementById("preview-play-btn").textContent = "Next caption";
    return;
  }
  isPlaying = !isPlaying;
  scheduleCaptionPreview();
  document.getElementById("preview-play-btn").textContent = isPlaying
    ? "Pause captions"
    : "Play captions";
  document
    .getElementById("preview-play-btn")
    .setAttribute("aria-pressed", String(isPlaying));
}
function validTiming(start, end) {
  return (
    Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start
  );
}
function setHookTiming(start, end) {
  const hook = CURRENT_HOOKS.find(
    (h) => String(h.id) === String(selectedHookId),
  );
  const alert = document.getElementById("timing-error");
  if (!hook) {
    alert.textContent = "Select a moment before editing timing.";
    return false;
  }
  if (!validTiming(start, end)) {
    const invalidStart = !Number.isFinite(start) || start < 0;
    alert.textContent = invalidStart
      ? "Start must be finite and zero or later."
      : "End must be finite and follow start.";
    alert.dataset.field = invalidStart ? "timing-start" : "timing-end";
    return false;
  }
  alert.textContent = "";
  Object.assign(hook, {
    start,
    end,
    start_fmt: formatSecondsToMMSS(start),
    end_fmt: formatSecondsToMMSS(end),
    duration: (end - start).toFixed(1) + "s",
  });
  persistStudio();
  updateSelectedHookDetails();
  renderHooksList();
  return true;
}
function applyTiming() {
  const startField = document.getElementById("timing-start"),
    endField = document.getElementById("timing-end");
  return setHookTiming(
    startField.value.trim() === "" ? NaN : Number(startField.value),
    endField.value.trim() === "" ? NaN : Number(endField.value),
  );
}
function nudgeTime(type, delta) {
  const hook = CURRENT_HOOKS.find(
    (h) => String(h.id) === String(selectedHookId),
  );
  if (hook)
    setHookTiming(
      type === "start" ? Number(hook.start) + delta : Number(hook.start),
      type === "end" ? Number(hook.end) + delta : Number(hook.end),
    );
}

function formatSecondsToMMSS(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s < 10 ? "0" : ""}${s}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
}
function appendConsoleLog(msg) {
  if (/\[[^\]]*error\]/i.test(msg)) {
    const alert = document.getElementById("studio-alert");
    alert.dataset.state = "error";
    alert.textContent = String(msg).replace(/^\[[^\]]*error\]\s*/i, "");
    alert.classList.remove("hidden");
  }
  const box = document.getElementById("live-engine-console");
  if (!box) return;
  const line = document.createElement("p");
  line.innerText = msg;
  box.appendChild(line);
  while (box.children.length > 200) box.firstElementChild.remove();
  box.scrollTop = box.scrollHeight;
}

function toggleTerminalLogs() {
  const consoleBox = document.getElementById("live-engine-console");
  const label = document.getElementById("terminal-toggle-label");
  consoleBox.classList.toggle("hidden");
  label.innerText = consoleBox.classList.contains("hidden")
    ? "Show console logs"
    : "Hide console logs";
}

// ==================== ENGINE API CALLS ====================

function updateJobControls() {
  const busy = jobStarting || Boolean(activeJobId) || jobsLoading || jobsFailed;
  const changedSource =
    sourceForHooks !== document.getElementById("source-url").value.trim();
  document.getElementById("source-state").textContent =
    CURRENT_HOOKS.length && changedSource
      ? "Source changed. Detect hooks before exporting its moments."
      : "";
  document.getElementById("analyze-btn").disabled = busy;
  document.getElementById("execute-make-btn").disabled = busy;
  document.getElementById("top-render-btn").disabled =
    busy || !CURRENT_HOOKS.length || changedSource;
  document.getElementById("source-url").disabled =
    jobStarting || Boolean(activeJobId);
  document.getElementById("clip-topic").disabled = jobStarting || Boolean(activeJobId);
  document.querySelectorAll(".caption-phrase textarea").forEach((field) => { field.disabled = busy || changedSource; });
  document.getElementById("caption-reset").disabled = busy || changedSource || !CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId))?.caption_edits?.length;
  document
    .querySelectorAll("[data-export-hook]")
    .forEach((button) => (button.disabled = busy || changedSource));
  document.getElementById("job-cancel-btn").hidden = !activeJobId;
  document.getElementById("job-cancel-btn").disabled = cancelPending;
}
function renderJobHistory() {
  const container = document.getElementById("jobs-list");
  container.replaceChildren();
  jobs.forEach((job) => {
    const row = document.createElement("li"),
      description = document.createElement("span"),
      title = document.createElement("strong"),
      detail = document.createElement("small"),
      status = document.createElement("span");
    title.textContent = job.title || job.url || job.target || job.type || "Job";
    detail.textContent = (job.type || "job") + " · " + job.id;
    description.append(title, detail);
    status.textContent =
      job.status +
      (!TERMINAL_JOBS.has(job.status)
        ? " · " + Math.round(Number(job.progress) || 0) + "%"
        : "");
    row.append(description, status);
    if (!TERMINAL_JOBS.has(job.status)) {
      const cancel = document.createElement("button");
      cancel.textContent = "Cancel";
      cancel.onclick = () => cancelJob(job.id);
      row.append(cancel);
    } else if (
      job.type === "analyze" &&
      job.status === "completed" &&
      job.result?.hooks
    ) {
      const restore = document.createElement("button");
      restore.textContent = "Open analysis";
      restore.disabled = Boolean(activeJobId);
      restore.onclick = () => {
        hydrateAnalysis(job);
        switchTab("studio");
      };
      row.append(restore);
    } else if (["failed", "cancelled", "interrupted"].includes(job.status)) {
      const retry = document.createElement("button");
      retry.textContent = "Retry job";
      retry.disabled = Boolean(activeJobId) || jobStarting;
      retry.onclick = () => retryJob(job);
      row.append(retry);
    }
    container.append(row);
  });
  if (!jobs.length) {
    const row = document.createElement("li");
    row.textContent = "No jobs yet. Detect hooks from a source video.";
    container.append(row);
  }
}
function rememberJob(job) {
  const index = jobs.findIndex((existing) => existing.id === job.id);
  if (index < 0) jobs.unshift(job);
  else jobs[index] = { ...jobs[index], ...job };
  const active = jobs.filter((job) => !TERMINAL_JOBS.has(job.status)),
    terminal = jobs.filter((job) => TERMINAL_JOBS.has(job.status));
  jobs = [...active, ...terminal.slice(0, 100)];
  renderJobHistory();
}
function showJob(job) {
  job = { ...jobs.find((existing) => existing.id === job.id), ...job };
  document.getElementById("pipeline-stepper").classList.remove("hidden");
  document.getElementById("pipeline-stepper").dataset.state = job.status;
  document.getElementById("active-task-id").textContent = job.id;
  document.getElementById("stepper-status-text").textContent =
    job.status + ": " + (job.step || job.type || "Working");
  const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
  document.getElementById("stepper-percent").textContent = progress + "%";
  document.getElementById("stepper-progress-fill").style.width = progress + "%";
  const logs = Array.isArray(job.logs) ? job.logs : [],
    offset = Number(job.log_offset) || 0,
    seen = jobLogOffsets.get(job.id) || 0;
  logs.slice(Math.max(0, seen - offset)).forEach(appendConsoleLog);
  jobLogOffsets.set(job.id, offset + logs.length);
  while (jobLogOffsets.size > 100)
    jobLogOffsets.delete(jobLogOffsets.keys().next().value);
  rememberJob(job);
  if (
    (job.result?.hooks?.length || (job.type === "analyze" && job.status === "completed" && Array.isArray(job.result?.hooks))) &&
    (selectedAnalysisId !== job.id || !CURRENT_HOOKS.length)
  )
    hydrateAnalysis(job);
}
function hydrateAnalysis(job, restoreEdits = false) {
  const source = job.url || job.target || job.result?.url || "";
  if (!source) return;
  sourceForHooks = source;
  document.getElementById("source-url").value = source;
  selectedAnalysisId = job.id;
  CURRENT_HOOKS = Array.isArray(job.result?.hooks)
    ? job.result.hooks.slice(0, 100).map((hook) => ({ ...hook }))
    : [];
  currentSourceVideoTitle = job.result?.video_title || "";
  currentAnalysisTopic = job.result?.topic || job.request?.topic || "";
  analysisNotice = job.result?.notice || "";
  document.getElementById("clip-topic").value = currentAnalysisTopic;
  if (restoreEdits)
    (window._restoredTiming || []).forEach((edit) => {
      if (!edit || typeof edit !== "object") return;
      const hook = CURRENT_HOOKS.find((h) => String(h.id) === String(edit.id));
      if (hook && validTiming(edit.start, edit.end))
        Object.assign(hook, { start: edit.start, end: edit.end,
          caption_edits: Array.isArray(edit.caption_edits) ? edit.caption_edits.slice(0, 200).filter((cue) =>
            cue && typeof cue === "object" && typeof cue.text === "string" && cue.text.length <= 1000 && !cue.text.includes("\0") &&
            hook.cues?.some((original) => original.start === cue.start && original.end === cue.end)) : [] }, {
          start_fmt: formatSecondsToMMSS(edit.start),
          end_fmt: formatSecondsToMMSS(edit.end),
          duration: (edit.end - edit.start).toFixed(1) + "s",
        });
    });
  if (!CURRENT_HOOKS.some((h) => String(h.id) === String(selectedHookId)))
    selectedHookId = CURRENT_HOOKS[0]?.id || null;
  autoDetectedFraming = job.result?.recommended_framing || "fit";
  autoDetectedStyle = job.result?.recommended_style || "gold";
  document.getElementById("smart-recommendation-text").textContent =
    "Auto-tuned: " +
    (autoDetectedFraming === "fit" ? "Canvas fit" : "Center crop") +
    " + " +
    autoDetectedStyle;
  applyVisualFramingState();
  renderHooksList();
  updateSelectedHookDetails();
  updateOnboarding();
  persistStudio();
}
async function finishJob(job) {
  job = { ...jobs.find((existing) => existing.id === job.id), ...job };
  if (
    (job.result?.hooks?.length || (job.type === "analyze" && job.status === "completed" && Array.isArray(job.result?.hooks))) &&
    (selectedAnalysisId !== job.id || !CURRENT_HOOKS.length)
  )
    hydrateAnalysis(job);
  if (job.status === "completed") {
    const alert = document.getElementById("studio-alert");
    alert.dataset.state = "success";
    alert.replaceChildren(
      document.createTextNode(
        job.type === "analyze"
          ? job.result?.topic && !job.result.hooks?.length ? "No topic matches. Try a broader topic or clear it to detect general hooks." : "Hooks ready. Select a moment to export."
          : "Export complete. Your clips are saved.",
      ),
    );
    if (job.type !== "analyze") {
      const folder = document.createElement("button"),
        clips = document.createElement("button");
      folder.textContent = "Open folder";
      folder.onclick = () => openOutputFolder(job.output_file, job.id, folder);
      clips.textContent = "View clips";
      clips.onclick = () => switchTab("gallery");
      alert.append(folder, clips);
    }
    alert.classList.remove("hidden");
  }
  if (job.status === "failed")
    appendConsoleLog(
      "[job error] " + (job.error || "Job failed. Review the logs and retry."),
    );
  if (job.status === "interrupted")
    appendConsoleLog(
      "[job error] Engine restarted during this job. Retry starts a new job.",
    );
  if (job.status === "cancelled")
    appendConsoleLog(
      "[job] Cancelled. Your source and selected moments are still here.",
    );
  persistStudio();
  renderHooksList();
  updateJobControls();
  renderJobHistory();
  updateOnboarding();
  if (job.status === "completed" && job.type !== "analyze") await loadOutputs();
}
async function loadJobs() {
  if (jobsLoading) return;
  jobsLoading = true;
  updateJobControls();
  const status = document.getElementById("jobs-status");
  status.textContent = "Loading job history…";
  document.getElementById("jobs-retry-btn").hidden = true;
  try {
    const data = await apiJson("/api/jobs");
    if (!Array.isArray(data.jobs))
      throw new Error("Invalid job history response");
    jobs = data.jobs;
    jobsFailed = false;
    const analysis = jobs.find(
      (job) => job.id === selectedAnalysisId && job.status === "completed",
    );
    if (analysis) {
      const currentSource = document.getElementById("source-url").value;
      hydrateAnalysis(analysis, true);
      if (currentSource !== sourceForHooks)
        document.getElementById("source-url").value = currentSource;
    }
    const previous = jobs.find((job) => job.id === activeJobId);
    if (previous && TERMINAL_JOBS.has(previous.status)) {
      activeJobId = null;
      showJob(previous);
      await finishJob(previous);
    } else
      activeJobId =
        (previous && !TERMINAL_JOBS.has(previous.status)
          ? previous
          : jobs.find((job) => !TERMINAL_JOBS.has(job.status))
        )?.id || null;
    status.textContent = jobs.length ? "Recent jobs" : "";
    renderJobHistory();
    persistStudio();
  } catch (error) {
    jobsFailed = true;
    status.textContent = "Couldn’t load job history. " + error.message;
    document.getElementById("jobs-retry-btn").hidden = false;
  } finally {
    jobsLoading = false;
    updateJobControls();
  }
  if (activeJobId && !jobsFailed) pollJobs();
}
async function pollJobs() {
  if (jobLoopRunning || pageLeaving) return;
  jobLoopRunning = true;
  let failures = 0;
  try {
    while (activeJobId && !pageLeaving) {
      const id = activeJobId,
        known = jobs.find((job) => job.id === id);
      pollController = new AbortController();
      const timeout = setTimeout(() => pollController?.abort(), 15000);
      try {
        const job = await apiJson(
          known?.type === "analyze"
            ? "/api/analyze/status/" + encodeURIComponent(id)
            : "/api/jobs/" + encodeURIComponent(id),
          { signal: pollController.signal },
        );
        if (activeJobId !== id || pageLeaving) continue;
        if (
          !job.status ||
          !["queued", "running", "cancelling", ...TERMINAL_JOBS].includes(
            job.status,
          )
        )
          throw new Error("Invalid job status response");
        job.id = job.id || id;
        job.type = job.type || known?.type;
        failures = 0;
        document.getElementById("job-retry-btn").hidden = true;
        showJob(job);
        if (TERMINAL_JOBS.has(job.status)) {
          activeJobId = null;
          await finishJob(jobs.find((existing) => existing.id === id) || job);
          activeJobId =
            jobs.find((other) => !TERMINAL_JOBS.has(other.status))?.id || null;
          persistStudio();
          updateJobControls();
        }
      } catch (error) {
        if (pageLeaving) break;
        if (activeJobId !== id) continue;
        failures++;
        const stop = failures >= 5 || [401, 403, 404].includes(error.status);
        document.getElementById("stepper-status-text").textContent =
          "Connection lost. " +
          error.message +
          (stop ? " Retry status to reconnect." : " Reconnecting…");
        if (stop) {
          document.getElementById("job-retry-btn").hidden = false;
          break;
        }
      } finally {
        clearTimeout(timeout);
        pollController = null;
      }
      if (activeJobId && !pageLeaving)
        await new Promise((resolve) =>
          setTimeout(resolve, Math.min(1000 * 2 ** failures, 10000)),
        );
    }
  } finally {
    jobLoopRunning = false;
  }
}
async function cancelJob(id = activeJobId) {
  if (!id || cancelPending) return;
  cancelPending = true;
  updateJobControls();
  try {
    const job = await apiJson(
      "/api/jobs/" + encodeURIComponent(id) + "/cancel",
      { method: "POST" },
    );
    job.id = job.id || id;
    job.type = job.type || jobs.find((existing) => existing.id === id)?.type;
    rememberJob(job);
    if (id === activeJobId) {
      showJob(job);
      if (TERMINAL_JOBS.has(job.status)) {
        activeJobId = null;
        pollController?.abort();
        await finishJob(job);
        activeJobId =
          jobs.find((other) => !TERMINAL_JOBS.has(other.status))?.id || null;
        if (activeJobId) pollJobs();
      }
    }
    persistStudio();
  } catch (error) {
    appendConsoleLog(
      "[cancel error] Couldn’t cancel the job. " +
        error.message +
        " Try Cancel again.",
    );
  } finally {
    cancelPending = false;
    updateJobControls();
  }
}
async function startJob(type, endpoint, payload) {
  if (jobStarting || activeJobId || jobsLoading || jobsFailed) return;
  document.getElementById("studio-alert").classList.add("hidden");
  jobStarting = true;
  updateJobControls();
  try {
    const data = await apiJson(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!data.job_id) throw new Error("Server did not return a valid job ID");
    activeJobId = data.job_id;
    if (type === "make" || type === "analyze") {
      sourceForHooks = payload.url || payload.target;
      CURRENT_HOOKS = [];
      selectedHookId = selectedAnalysisId = null;
      currentSourceVideoTitle = "";
      currentAnalysisTopic = payload.topic || "";
      analysisNotice = "";
      document.getElementById("caption-search").value = "";
      renderHooksList();
      updateSelectedHookDetails();
    }
    showJob({
      id: activeJobId,
      type,
      status: "queued",
      progress: 0,
      step: "Waiting for engine",
      url: payload.url || payload.target,
      title: payload.title,
    });
    persistStudio();
  } catch (error) {
    appendConsoleLog(
      "[job error] " +
        error.message +
        " Checking history before another launch.",
    );
    await loadJobs();
  } finally {
    jobStarting = false;
    updateJobControls();
  }
  if (activeJobId) pollJobs();
}
async function triggerAnalyze() {
  const url = document.getElementById("source-url").value.trim();
  if (!url) return appendConsoleLog("[error] Paste a source video URL first.");
  persistStudio();
  const topic = document.getElementById("clip-topic").value.trim();
  await startJob("analyze", "/api/analyze/start", { url, ...(topic ? { topic } : {}) });
}
async function renderSelectedHook() {
  const hook =
    CURRENT_HOOKS.find((h) => String(h.id) === String(selectedHookId)) ||
    CURRENT_HOOKS[0];
  if (!hook)
    return appendConsoleLog("[error] Detect hooks and select a moment first.");
  await renderSingleHookById(hook.id);
}
async function renderSingleHookById(hookId) {
  if (String(hookId) === String(selectedHookId) && !applyTiming()) {
    document
      .getElementById(document.getElementById("timing-error").dataset.field)
      ?.focus();
    return;
  }
  const hook = CURRENT_HOOKS.find((h) => String(h.id) === String(hookId)),
    url = document.getElementById("source-url").value.trim();
  if (!hook || !url || url !== sourceForHooks)
    return appendConsoleLog(
      "[error] Analyze this source before exporting its moments.",
    );
  if (!validTiming(Number(hook.start), Number(hook.end)))
    return appendConsoleLog(
      "[error] Correct clip start and end before exporting.",
    );
  if (hook.caption_edits?.some((edit) => !edit.text.trim() || edit.text.length > 1000)) {
    selectHook(hook.id);
    appendConsoleLog("[error] Correct empty or oversized caption text before exporting, or reset captions.");
    document.querySelector('#dialogue-lines-box textarea[aria-invalid="true"]')?.focus();
    return;
  }
  await startJob("render_clip", "/api/jobs/render_clip", {
    url,
    start: Number(hook.start),
    end: Number(hook.end),
    style:
      activeSubtitleMode === "auto" ? autoDetectedStyle : activeSubtitleMode,
    framing:
      activeFramingMode === "auto" ? autoDetectedFraming : activeFramingMode,
    title: hook.title,
    video_title: currentSourceVideoTitle,
    ...(hook.caption_edits?.length ? { caption_edits: hook.caption_edits } : {}),
  });
}
async function triggerMakePipeline() {
  const target = document.getElementById("source-url").value.trim();
  if (!target)
    return appendConsoleLog(
      "[error] Paste a source URL before running the pipeline.",
    );
  await startJob("make", "/api/jobs/make", {
    target,
    framing:
      activeFramingMode === "auto" ? autoDetectedFraming : activeFramingMode,
    style:
      activeSubtitleMode === "auto" ? autoDetectedStyle : activeSubtitleMode,
  });
}
async function retryJob(job) {
  job = { ...job, ...job.request };
  const source = job.url || job.target;
  if (!source)
    return appendConsoleLog(
      "[error] Paste the source again to retry this job.",
    );
  document.getElementById("source-url").value = source;
  switchTab("studio");
  if (job.type === "analyze") {
    document.getElementById("clip-topic").value = job.topic || "";
    return triggerAnalyze();
  }
  if (job.type === "make") return triggerMakePipeline();
  if (!validTiming(Number(job.start), Number(job.end)))
    return appendConsoleLog("[error] Select a moment and export it again.");
  await startJob("render_clip", "/api/jobs/render_clip", {
    url: source,
    start: Number(job.start),
    end: Number(job.end),
    title: job.title || "",
    video_title: job.video_title || "",
    framing: job.framing || "fit",
    style: job.style || "gold",
    ...(job.caption_edits ? { caption_edits: job.caption_edits } : {}),
  });
}
window.addEventListener("pagehide", () => {
  pageLeaving = true;
  clearTimeout(captionTimer);
  pollController?.abort();
});

// ==================== SETTINGS & .ENV MANAGEMENT ====================

function setEnvStatus(message, state = "ready") {
  const status = document.getElementById("env-status-alert");
  status.classList.remove("hidden");
  status.textContent = message;
  status.dataset.state = state;
  document.getElementById("env-save-btn").disabled =
    !envLoaded || envSaving || envLoading || envSwitching;
  document.getElementById("env-retry-btn").hidden =
    state !== "error" || envDirty;
  document.getElementById("env-discard-btn").hidden = !envDirty;
  document
    .querySelectorAll(
      "#env-form-view input,#env-form-view select,#env-raw-textarea,#env-tab-form,#env-tab-raw",
    )
    .forEach(
      (field) =>
        (field.disabled =
          !envLoaded || envSaving || envLoading || envSwitching),
    );
}
function envFormValues() {
  return Object.fromEntries(
    Object.entries(ENV_FIELDS)
      .map(([key, id]) => [key, document.getElementById(id).value])
      .filter(([key, value]) => value !== envFormBaseline[key]),
  );
}
function applyEnvDocument(data) {
  if (
    typeof data.raw !== "string" ||
    !data.values ||
    typeof data.values !== "object"
  )
    throw new Error("Invalid settings response");
  cachedEnvConfig = { raw: data.raw, values: data.values };
  for (const [key, id] of Object.entries(ENV_FIELDS)) {
    const field = document.getElementById(id),
      value = data.values[key] || "";
    if (
      field.tagName === "SELECT" &&
      value &&
      ![...field.options].some((option) => option.value === value)
    ) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      field.append(option);
    }
    field.value = value;
  }
  envFormBaseline = Object.fromEntries(
    Object.entries(ENV_FIELDS).map(([key, id]) => [
      key,
      document.getElementById(id).value,
    ]),
  );
  document.getElementById("env-raw-textarea").value = data.raw;
  updateOnboarding();
}
async function loadEnvConfig(discard = false) {
  if (envLoading || envSaving || (envDirty && !discard)) return;
  if (discard) {
    envDirty = false;
    envRevision++;
  }
  envLoading = true;
  setEnvStatus("Loading settings…", "loading");
  try {
    applyEnvDocument(await apiJson("/api/env"));
    envDraftOperations = [];
    envEditorDirty = false;
    envLoaded = true;
    envDirty = false;
    setEnvStatus(
      "Settings loaded. Saves apply to new jobs; running jobs keep their starting settings.",
    );
  } catch (error) {
    setEnvStatus(
      "Couldn’t load settings. " + error.message + " Retry loading.",
      "error",
    );
  } finally {
    envLoading = false;
    setEnvStatus(
      document.getElementById("env-status-alert").textContent,
      document.getElementById("env-status-alert").dataset.state,
    );
  }
}
function currentEnvOperation() {
  return envTab === "raw"
    ? { raw: document.getElementById("env-raw-textarea").value }
    : { values: envFormValues() };
}
function draftPayload() {
  const draft = [...envDraftOperations];
  if (envEditorDirty || !draft.length) draft.push(currentEnvOperation());
  return { draft };
}
async function saveEnvForm() {
  if (!envLoaded || envSaving || envLoading || envSwitching) return;
  envSaving = true;
  setEnvStatus("Saving configuration…", "saving");
  try {
    const payload = draftPayload();
    const data = await apiJson("/api/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applyEnvDocument(data);
    envDraftOperations = [];
    envEditorDirty = false;
    envDirty = false;
    const restart =
      Array.isArray(data.restart_required) && data.restart_required.length
        ? " Restart the engine for: " + data.restart_required.join(", ") + "."
        : "";
    setEnvStatus(
      "Configuration saved for new jobs. Running jobs keep their starting settings." +
        restart,
      "saved",
    );
  } catch (error) {
    setEnvStatus(
      "Couldn’t save configuration. Your edits are still here. " +
        error.message,
      "error",
    );
  } finally {
    envSaving = false;
    setEnvStatus(
      document.getElementById("env-status-alert").textContent,
      document.getElementById("env-status-alert").dataset.state,
    );
  }
}
async function switchEnvTab(mode) {
  if (mode === envTab || envSwitching || envSaving || !envLoaded) return;
  if (envEditorDirty && envDraftOperations.length >= 99) {
    setEnvStatus(
      "Save this draft before switching editors again. Your edits are still here.",
      "error",
    );
    return;
  }
  if (envEditorDirty) {
    envSwitching = true;
    setEnvStatus("Checking draft before switching editors…", "loading");
    try {
      const payload = draftPayload();
      const data = await apiJson("/api/env/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      applyEnvDocument(data);
      envDraftOperations = payload.draft;
      envEditorDirty = false;
    } catch (error) {
      setEnvStatus(
        "Couldn’t switch editors. Your draft is still here. " + error.message,
        "error",
      );
      return;
    } finally {
      envSwitching = false;
      setEnvStatus(
        document.getElementById("env-status-alert").textContent,
        document.getElementById("env-status-alert").dataset.state,
      );
    }
  }
  envTab = mode;
  const isRaw = mode === "raw";
  document.getElementById("env-form-view").classList.toggle("hidden", isRaw);
  document.getElementById("env-raw-view").classList.toggle("hidden", !isRaw);
  document
    .getElementById("env-tab-form")
    .setAttribute("aria-pressed", String(!isRaw));
  document
    .getElementById("env-tab-raw")
    .setAttribute("aria-pressed", String(isRaw));
  document
    .getElementById("env-tab-form")
    .classList.toggle("env-tab-active", !isRaw);
  document
    .getElementById("env-tab-raw")
    .classList.toggle("env-tab-active", isRaw);
  setEnvStatus(
    envDirty
      ? "Unsaved draft. Switching editors did not save it."
      : "Settings loaded.",
    envDirty ? "unsaved" : "ready",
  );
}

function togglePasswordVisibility(fieldId, btn) {
  const field = document.getElementById(fieldId);
  if (field.type === "password") {
    field.type = "text";
    btn.innerHTML = `<i data-lucide="eye-off" class="w-4 h-4"></i>`;
  } else {
    field.type = "password";
    btn.innerHTML = `<i data-lucide="eye" class="w-4 h-4"></i>`;
  }
  lucide.createIcons();
}

// ==================== CHANNELS.TXT RADAR ====================
function channelsDirty() {
  return channelsLoaded && JSON.stringify(cachedChannels) !== savedChannels;
}

function setChannelsStatus(message, state = "idle", retry = false) {
  const status = document.getElementById("channels-status");
  status.textContent = message;
  status.dataset.state = state;
  document.getElementById("channels-retry-btn").hidden = !retry;
  document.getElementById("channels-save-btn").disabled =
    !channelsLoaded || channelsSaving || !channelsDirty();
  document.getElementById("channels-save-label").textContent = channelsSaving
    ? "Saving…"
    : "Save channels";
  document.getElementById("new-channel-input").disabled =
    !channelsLoaded || channelsSaving;
  document.getElementById("channels-add-btn").disabled =
    !channelsLoaded || channelsSaving;
  document.querySelectorAll("#channels-table-body button").forEach((button) => {
    button.disabled = channelsSaving;
  });
}

async function loadChannels() {
  if (channelsSaving || channelsDirty()) return;
  channelsLoaded = false;
  setChannelsStatus("Loading channels…", "loading");
  try {
    const res = await fetch("/api/channels", {
      signal: AbortSignal.timeout(15000),
    });
    if (!res.ok) throw new Error("Channel request failed");
    const data = await res.json();
    if (
      !Array.isArray(data.channels) ||
      data.channels.some((line) => typeof line !== "string")
    ) {
      throw new Error("Invalid channel response");
    }
    cachedChannels = data.channels;
    savedChannels = JSON.stringify(cachedChannels);
    channelsLoaded = true;
    renderChannelsTable();
    setChannelsStatus(
      cachedChannels.length
        ? "Channels are up to date."
        : "Add a YouTube channel to get started.",
    );
  } catch (e) {
    document.getElementById("channels-table-body").replaceChildren();
    setChannelsStatus(
      "Couldn’t load channels. Check that Hooky is running, then retry.",
      "error",
      true,
    );
  }
}

function renderChannelsTable() {
  const tbody = document.getElementById("channels-table-body");
  if (!tbody) return;

  if (cachedChannels.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="p-6 text-center text-slate-500 font-mono">No channels configured in channels.txt</td></tr>`;
    return;
  }

  tbody.innerHTML = cachedChannels
    .map((line, idx) => {
      const parts = line.split("|").map((s) => s.trim());
      const handle = parts[0] || "";
      const clips = parts.find((p) => p.startsWith("limit=")) || "limit=auto";
      const focus =
        parts
          .slice(1)
          .filter((p) => !p.startsWith("limit="))
          .join(" | ") || "Defaults";

      return `
          <tr class="hover:bg-white/[0.02] transition-colors">
            <td class="p-3 font-mono font-medium text-white">${escapeHtml(handle)}</td>
            <td class="p-3 text-slate-400 font-mono tabular">${escapeHtml(clips)}</td>
            <td class="p-3 text-slate-400 font-mono">${escapeHtml(focus)}</td>
            <td class="p-3 text-right">
              <button onclick="removeChannel(${idx})" aria-label="Remove ${escapeHtml(handle)}" class="text-red-400 hover:text-red-300 font-mono text-[11px]">
                Remove
              </button>
            </td>
          </tr>
        `;
    })
    .join("");
}

function addNewChannel() {
  if (!channelsLoaded || channelsSaving) return;
  const input = document.getElementById("new-channel-input");
  const val = input.value.trim();
  if (!val) {
    input.focus();
    setChannelsStatus("Enter a YouTube channel handle or URL.", "error");
    return;
  }
  if (cachedChannels.includes(val)) {
    setChannelsStatus("This channel is already in the list.", "error");
    return;
  }
  cachedChannels.push(val);
  input.value = "";
  renderChannelsTable();
  setChannelsStatus(
    "Unsaved changes. Save channels to keep this list.",
    "unsaved",
  );
  input.focus();
}

function removeChannel(idx) {
  if (!channelsLoaded || channelsSaving) return;
  cachedChannels.splice(idx, 1);
  renderChannelsTable();
  setChannelsStatus(
    channelsDirty()
      ? "Unsaved changes. Save channels to keep this list."
      : "Channels are up to date.",
    channelsDirty() ? "unsaved" : "idle",
  );
}

async function saveActiveChannels() {
  if (!channelsLoaded || channelsSaving || !channelsDirty()) return;
  channelsSaving = true;
  setChannelsStatus("Saving channels…", "saving");
  try {
    const res = await fetch("/api/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channels: cachedChannels }),
      signal: AbortSignal.timeout(15000),
    });
    if (!res.ok) throw new Error("Channel save failed");
    savedChannels = JSON.stringify(cachedChannels);
    channelsSaving = false;
    setChannelsStatus("Channels saved.", "saved");
    appendConsoleLog("[channels] Successfully saved channels.txt");
  } catch (e) {
    channelsSaving = false;
    setChannelsStatus(
      "Couldn’t save channels. Your edits are still here. Try Save channels again.",
      "error",
    );
  }
}

window.addEventListener("beforeunload", (event) => {
  if (channelsDirty() || envDirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

// ==================== GALLERY & OUTPUTS ====================

async function loadOutputs() {
  if (outputsLoading) return;
  outputsLoading = true;
  const status = document.getElementById("outputs-status");
  status.textContent = "Loading clips…";
  status.dataset.state = "loading";
  document.getElementById("outputs-refresh-btn").disabled = true;
  document.getElementById("outputs-retry-btn").hidden = true;
  try {
    const data = await apiJson("/api/outputs");
    if (!Array.isArray(data.files)) throw new Error("Invalid gallery response");
    const files = data.files;
    const downloadAll = document.getElementById("outputs-download-all");
    downloadAll.hidden = !files.length;
    downloadAll.href = outputVideoUrl("", "/api/outputs/download-all");
    document.getElementById("gallery-count").textContent = files.length;
    const container = document.getElementById("outputs-grid");
    container.replaceChildren();
    if (!files.length) {
      const empty = document.createElement("p");
      empty.textContent =
        "No clips yet. Detect hooks, select a moment, and export your first clip.";
      container.append(empty);
    } else {
      const folders =
        Array.isArray(data.folders) && data.folders.length
          ? data.folders
          : [{ name: "Your clips", files }];
      folders.forEach((folder) => {
        const group = document.createElement("section"),
          heading = document.createElement("h3"),
          grid = document.createElement("div");
        group.className = "output-collection";
        heading.textContent = folder.name;
        const folderActions = document.createElement("div"),
          open = document.createElement("button");
        folderActions.className = "output-actions";
        open.textContent = "Open folder";
        const first = folder.files?.[0];
        open.onclick = () =>
          openOutputFolder(
            first?.rel_path,
            first?.url
              ? new URL(first.url, location.href).searchParams.get("job_id")
              : null,
            open,
          );
        folderActions.append(open);
        grid.className = "output-card-grid";
        (folder.files || []).forEach((file) => {
          const card = document.createElement("article"),
            name = document.createElement("h4"),
            meta = document.createElement("p"),
            actions = document.createElement("div"),
            watch = document.createElement("button"),
            download = document.createElement("a"),
            outputUrl = outputVideoUrl(file.rel_path, file.url);
          card.className = "output-card";
          name.textContent = file.name;
          meta.textContent =
            String(file.size_mb) +
            " MB · " +
            new Date(file.mtime * 1000).toLocaleString();
          watch.textContent = "Watch";
          watch.onclick = () =>
            playOutputVideo(file.rel_path, file.name, outputUrl);
          download.textContent = "Download MP4";
          download.href = outputUrl;
          download.download = "";
          actions.className = "output-actions";
          actions.append(watch, download);
          card.append(name, meta, actions);
          grid.append(card);
        });
        group.append(heading, folderActions, grid);
        container.append(group);
      });
    }
    status.textContent = files.length
      ? files.length + " exported " + (files.length === 1 ? "clip" : "clips")
      : "Gallery loaded.";
    status.dataset.state = "ready";
    if (files.length) {
      document.getElementById("setup-export").textContent =
        "First clip exported";
    }
  } catch (error) {
    status.textContent =
      "Couldn’t load clips. " +
      error.message +
      " Retry loading; previously loaded clips remain here.";
    status.dataset.state = "error";
    document.getElementById("outputs-retry-btn").hidden = false;
  } finally {
    outputsLoading = false;
    document.getElementById("outputs-refresh-btn").disabled = false;
  }
}

async function openOutputFolder(filepath, jobId, button) {
  if (button) button.disabled = true;
  const query = new URLSearchParams();
  if (filepath) query.set("filepath", filepath);
  if (jobId) query.set("job_id", jobId);
  try {
    await apiJson("/api/outputs/open-folder?" + query, { method: "POST" });
    document.getElementById("outputs-status").textContent =
      "Opened your clips folder.";
  } catch (error) {
    document.getElementById("outputs-status").textContent = error.message;
    document.getElementById("outputs-status").dataset.state = "error";
    appendConsoleLog("[folder error] " + error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

function outputVideoUrl(filepath, outputUrl) {
  const encodedPath = String(filepath)
    .replace(/\\/g, "/")
    .split("/")
    .map(encodeURIComponent)
    .join("/");
  const url = new URL(
    outputUrl || "/api/outputs/" + encodedPath,
    window.location.href,
  );
  if (
    url.origin !== window.location.origin ||
    !url.pathname.startsWith("/api/outputs/") ||
    url.username ||
    url.password
  )
    throw new Error("Invalid clip URL");
  if (window.__HOOKY_TOKEN__)
    url.searchParams.set("token", window.__HOOKY_TOKEN__);
  return url.pathname + url.search;
}

function playOutputVideo(filepath, title, outputUrl) {
  const src = outputVideoUrl(filepath, outputUrl);
  outputPlayerTrigger = document.activeElement;
  const box = document.getElementById("active-video-player-box");
  box.classList.remove("hidden");
  document.getElementById("active-playing-title").innerText = title || filepath;

  const player = document.getElementById("html5-video-player");
  const srcElem = document.getElementById("video-source-elem");
  srcElem.src = src;
  player.load();
  player.play().catch(() => {});
  box.scrollIntoView({
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "instant"
      : "smooth",
  });
  document.getElementById("gallery-close-btn").focus();
}

function closeActivePlayer() {
  const box = document.getElementById("active-video-player-box");
  box.classList.add("hidden");
  const player = document.getElementById("html5-video-player");
  player.pause();
  if (outputPlayerTrigger?.isConnected) outputPlayerTrigger.focus();
  outputPlayerTrigger = null;
}

// ==================== SYSTEM DOCTOR ====================
async function loadDoctorReport() {
  try {
    document.getElementById("doctor-status").textContent =
      "Checking system health…";
    const data = await apiJson("/api/doctor");
    if (!Array.isArray(data.checks))
      throw new Error("Invalid system health response");
    doctorReady = data.checks.every(
      (check) =>
        check.ok || check.required === false || /key|gemini/i.test(check.name),
    );
    document.getElementById("doctor-status").textContent = doctorReady
      ? "Local checks complete. Configure an API key in Settings, then analyze a source."
      : "Resolve the failed required checks, then run system health again.";
    updateOnboarding();
    const container = document.getElementById("doctor-checks-container");
    if (!container) return;

    if (data.gpu) {
      document.getElementById("gpu-text").innerText = data.gpu;
    }

    const checks = data.checks || [];
    container.innerHTML = checks
      .map(
        (c) => `
          <div class="p-3 bg-[var(--hooky-panel)] border border-white/[0.08] rounded-md flex items-center justify-between text-xs font-mono">
            <div class="flex items-center gap-2">
              <span class="${c.ok ? "text-[var(--hooky-accent)]" : c.required === false ? "text-slate-400" : "text-red-400"} font-medium">
                ${c.ok ? "✓ pass" : c.required === false ? "optional" : "✗ fail"}
              </span>
              <span class="text-white font-medium">${escapeHtml(c.name)}</span>
            </div>
            <span class="text-slate-400 text-[11px] truncate max-w-[200px]" title="${escapeHtml(c.detail)}">${escapeHtml(c.detail)}</span>
          </div>
        `,
      )
      .join("");
  } catch (e) {
    document.getElementById("doctor-status").textContent =
      "Couldn’t check system health. Retry checks. " + e.message;
  }
}
