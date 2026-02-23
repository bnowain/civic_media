/**
 * index.js — Library page logic.
 * Unified landing page with sidebar filters, tabs (Meetings/Audio/News),
 * recent media sidebar, and radio show ingest system.
 */

"use strict";

// ── State ────────────────────────────────────────────────────────────────────

let activeMode = "meetings";          // "meetings" | "audio" | "news"
let governingBodyId = null;           // null = all
let recentSort = "processed";         // "processed" | "event"
let selectedCategory = "meeting";     // dialog category
let governingBodies = [];             // cached list
let editMeetingId = null;             // meeting being edited

const activePollers = {};
const POLL_INTERVAL = 4000;
let ingestPoller = null;

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  // Parse URL state
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab")) activeMode = params.get("tab");
  if (params.get("gb")) governingBodyId = params.get("gb");
  if (params.get("sort")) recentSort = params.get("sort");

  // Set active tab
  document.querySelectorAll(".lib-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.mode === activeMode);
  });

  // Set active sort toggle
  document.querySelectorAll("#recent-toggle .toggle-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.sort === recentSort);
  });

  const today = new Date().toISOString().slice(0, 10);
  const dateFields = ["f-meeting-date", "f-audio-date", "f-air-date"];
  dateFields.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = today;
  });

  // Load data
  await Promise.all([loadGoverningBodies(), loadRecent(), loadContent()]);

  // Event listeners — New Item dialog
  document.getElementById("new-item-btn").addEventListener("click", openDialog);
  document.getElementById("close-dialog-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-dialog-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("create-btn").addEventListener("click", handleCreate);
  document.getElementById("overlay").addEventListener("click", closeAllDialogs);
  document.getElementById("f-title").addEventListener("keydown", e => {
    if (e.key === "Enter") handleCreate();
  });
  document.getElementById("add-gb-btn").addEventListener("click", handleAddGoverningBody);

  // Edit dialog
  document.getElementById("close-edit-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-edit-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("save-edit-btn").addEventListener("click", handleSaveEdit);

  // Ingest dialog
  document.getElementById("close-ingest-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-ingest-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("run-ingest-btn").addEventListener("click", handleRunIngest);

  // Tabs
  document.getElementById("library-tabs").addEventListener("click", e => {
    const tab = e.target.closest(".lib-tab");
    if (!tab) return;
    activeMode = tab.dataset.mode;
    document.querySelectorAll(".lib-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");

    // Show/hide sidebar sections based on tab
    const stationsSection = document.getElementById("stations-section");
    const gbSection = document.getElementById("gb-list").parentElement;
    if (activeMode === "news") {
      stationsSection.style.display = "";
      gbSection.style.display = "none";
    } else {
      stationsSection.style.display = "none";
      gbSection.style.display = "";
    }

    updateToolbar();
    updateURL();
    loadContent();
  });

  // Recent sort toggle
  document.getElementById("recent-toggle").addEventListener("click", e => {
    const btn = e.target.closest(".toggle-btn");
    if (!btn) return;
    recentSort = btn.dataset.sort;
    document.querySelectorAll("#recent-toggle .toggle-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    updateURL();
    loadRecent();
  });

  // Category toggle in dialog
  document.getElementById("category-toggle").addEventListener("click", e => {
    const btn = e.target.closest(".cat-btn");
    if (!btn) return;
    selectedCategory = btn.dataset.cat;
    document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    updateDialogFields();
  });

  // Shutdown button
  document.getElementById("shutdown-btn").addEventListener("click", handleShutdown);

  updateToolbar();
}

// ── Shutdown ──────────────────────────────────────────────────────────────────

async function handleShutdown() {
  if (!confirm("Shut down Civic Media server? This will stop all background processes.")) return;
  try {
    await fetch("/api/system/shutdown", { method: "POST" });
  } catch { /* connection drops on shutdown */ }
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#888;"><div style="text-align:center"><h2>Server stopped</h2><p>Close this tab or restart with .\\start.ps1</p></div></div>';
}

// ── URL State ────────────────────────────────────────────────────────────────

function updateURL() {
  const params = new URLSearchParams();
  if (activeMode !== "meetings") params.set("tab", activeMode);
  if (governingBodyId) params.set("gb", governingBodyId);
  if (recentSort !== "processed") params.set("sort", recentSort);
  const qs = params.toString();
  history.pushState(null, "", qs ? `/?${qs}` : "/");
}

// ── Toolbar ──────────────────────────────────────────────────────────────────

function updateToolbar() {
  const actions = document.getElementById("toolbar-actions");
  if (!actions) return;

  if (activeMode === "audio") {
    actions.innerHTML = `<button class="btn btn-amber btn-sm" id="ingest-btn">Ingest Radio Shows</button>`;
    document.getElementById("ingest-btn").addEventListener("click", openIngestDialog);
  } else {
    actions.innerHTML = "";
  }
}

// ── API ──────────────────────────────────────────────────────────────────────

async function loadGoverningBodies() {
  try {
    const r = await fetch("/api/governing-bodies/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    governingBodies = await r.json();
    renderGoverningBodies();
  } catch (err) {
    console.error("Failed to load governing bodies:", err);
  }
}

async function loadRecent() {
  try {
    const r = await fetch(`/api/library/recent?limit=5&sort=${recentSort}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const items = await r.json();
    renderRecent(items);
  } catch (err) {
    console.error("Failed to load recent media:", err);
    document.getElementById("recent-list").innerHTML =
      '<div class="loading-state-sm">Failed to load</div>';
  }
}

async function loadContent() {
  const grid = document.getElementById("content-grid");
  grid.innerHTML = '<div class="loading-state">Loading...</div>';

  // Stop existing pollers
  Object.keys(activePollers).forEach(id => {
    clearInterval(activePollers[id]);
    delete activePollers[id];
  });

  try {
    if (activeMode === "news") {
      await loadNewsContent(grid);
    } else {
      await loadMeetingContent(grid);
    }
  } catch (err) {
    console.error("Failed to load content:", err);
    grid.innerHTML = '<div class="loading-state">Failed to load. Is the server running?</div>';
  }
}

async function loadMeetingContent(grid) {
  const category = activeMode === "audio" ? "audio" : "meeting";
  let url = `/api/meetings/?category=${category}`;
  if (governingBodyId) url += `&governing_body_id=${governingBodyId}`;

  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const items = await r.json();

  const label = activeMode === "audio" ? "item" : "meeting";
  document.getElementById("item-count").textContent =
    `${items.length} ${label}${items.length !== 1 ? "s" : ""}`;

  if (items.length === 0) {
    const msg = activeMode === "audio"
      ? "No audio transcriptions yet."
      : "No meetings yet. Create your first meeting to get started.";
    grid.innerHTML = `<div class="empty-state">${msg}</div>`;
    return;
  }

  grid.innerHTML = "";
  items.forEach(m => {
    const card = createMeetingCard(m);
    grid.appendChild(card);
  });

  // Kick off status checks
  items.forEach(m => updateMeetingCardStatus(m.meeting_id));
}

async function loadNewsContent(grid) {
  const r = await fetch("/api/news/");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const items = await r.json();

  document.getElementById("item-count").textContent =
    `${items.length} newscast${items.length !== 1 ? "s" : ""}`;

  if (items.length === 0) {
    grid.innerHTML = '<div class="empty-state">No news recordings yet. Add one to get started.</div>';
    return;
  }

  grid.innerHTML = "";
  items.forEach(n => {
    const card = createNewsCard(n);
    grid.appendChild(card);
  });
}

async function getMeetingStatus(meetingId) {
  try {
    const r = await fetch(`/api/media/${meetingId}/status`);
    if (!r.ok) return null;
    return r.json();
  } catch { return null; }
}

// ── Render: Sidebar ──────────────────────────────────────────────────────────

function renderGoverningBodies() {
  const list = document.getElementById("gb-list");
  list.innerHTML = '<div class="gb-item active" data-gb-id="">All</div>';

  governingBodies.forEach(gb => {
    const el = document.createElement("div");
    el.className = "gb-item";
    el.dataset.gbId = gb.governing_body_id;
    el.textContent = gb.display_name || gb.name;
    if (governingBodyId === gb.governing_body_id) {
      el.classList.add("active");
      list.querySelector('[data-gb-id=""]').classList.remove("active");
    }
    list.appendChild(el);
  });

  // Also populate the dialog dropdown
  const select = document.getElementById("f-governing-body");
  select.innerHTML = '<option value="">Select...</option>';
  governingBodies.forEach(gb => {
    const opt = document.createElement("option");
    opt.value = gb.governing_body_id;
    opt.textContent = gb.display_name || gb.name;
    select.appendChild(opt);
  });

  // Click handler for sidebar filtering
  list.addEventListener("click", e => {
    const item = e.target.closest(".gb-item");
    if (!item) return;
    governingBodyId = item.dataset.gbId || null;
    list.querySelectorAll(".gb-item").forEach(i => i.classList.remove("active"));
    item.classList.add("active");
    updateURL();
    loadContent();
  });
}

function renderRecent(items) {
  const list = document.getElementById("recent-list");

  if (items.length === 0) {
    list.innerHTML = '<div class="loading-state-sm">No recent media</div>';
    return;
  }

  list.innerHTML = "";
  items.forEach(item => {
    const el = document.createElement("div");
    el.className = "recent-item";

    const typeIcon = item.media_type === "news" ? "TV"
      : item.media_type === "audio" ? "AU" : "MT";

    el.innerHTML = `
      <span class="recent-type-badge recent-type-${item.media_type}">${typeIcon}</span>
      <div class="recent-info">
        <div class="recent-title">${esc(item.title)}</div>
        <div class="recent-sub">${esc(item.subtitle || "")} ${esc(formatDate(item.date))}</div>
      </div>
    `;

    el.addEventListener("click", () => {
      if (item.media_type === "news") {
        window.location.href = `/news/${item.id}`;
      } else {
        window.location.href = `/review/${item.id}`;
      }
    });

    list.appendChild(el);
  });
}

// ── Render: Cards ────────────────────────────────────────────────────────────

function createMeetingCard(m) {
  const card = document.createElement("div");
  card.className = "meeting-card";
  card.dataset.meetingId = m.meeting_id;

  const displayDate = formatDate(m.meeting_date || "\u2014");
  const isAudio = m.category === "audio";

  const subLine = isAudio
    ? (m.governing_body ? esc(m.governing_body) : "Audio Transcription")
    : `${esc(m.governing_body)} \u00b7 ${esc(m.meeting_type)}`;

  const descLine = m.description
    ? `<div class="meeting-card-desc">${esc(m.description)}</div>` : "";

  // Thumbnail for ingested audio
  const thumbHtml = m.thumbnail_url
    ? `<img class="meeting-card-thumb" src="${esc(m.thumbnail_url)}" alt="" loading="lazy">`
    : "";

  card.innerHTML = `
    ${thumbHtml}
    <span class="meeting-card-date">${esc(displayDate)}</span>
    <div class="meeting-card-body">
      <div class="meeting-card-title">${esc(m.title)}</div>
      <div class="meeting-card-sub">${subLine}</div>
      ${descLine}
      <div class="meeting-progress" id="progress-${m.meeting_id}"></div>
    </div>
    <span class="meeting-card-badge badge-pending" id="badge-${m.meeting_id}">\u2014</span>
    <div class="meeting-card-actions">
      <button class="btn btn-ghost btn-sm edit-btn" data-meeting-id="${m.meeting_id}" title="Edit">&#x270E;</button>
      <button class="btn btn-ghost btn-sm process-btn" data-meeting-id="${m.meeting_id}" title="Process" style="display:none">&#x25B6;</button>
      <button class="btn btn-ghost btn-sm delete-btn" data-meeting-id="${m.meeting_id}" title="Delete">&#x2715;</button>
    </div>
  `;

  // Store meeting data on the card for edit dialog
  card._meetingData = m;

  card.addEventListener("click", e => {
    if (e.target.closest(".delete-btn") || e.target.closest(".edit-btn") || e.target.closest(".process-btn")) return;
    window.location.href = `/review/${m.meeting_id}`;
  });

  card.querySelector(".delete-btn").addEventListener("click", async e => {
    e.stopPropagation();
    if (!confirm(`Delete "${m.title}"? This cannot be undone.`)) return;
    const r = await fetch(`/api/meetings/${m.meeting_id}`, { method: "DELETE" });
    if (r.ok) {
      loadContent();
      loadRecent();
    } else {
      alert("Failed to delete meeting.");
    }
  });

  card.querySelector(".edit-btn").addEventListener("click", e => {
    e.stopPropagation();
    openEditDialog(m);
  });

  card.querySelector(".process-btn").addEventListener("click", async e => {
    e.stopPropagation();
    if (!confirm(`Start processing "${m.title}"? This will use GPU resources.`)) return;
    const r = await fetch(`/api/media/${m.meeting_id}/process`, { method: "POST" });
    if (r.ok) {
      loadContent();
    } else {
      const err = await r.json().catch(() => ({}));
      alert(err.detail || "Failed to start processing.");
    }
  });

  return card;
}

function createNewsCard(n) {
  const card = document.createElement("div");
  card.className = "meeting-card";

  const displayDate = formatDate(n.air_date || "\u2014");
  const statusClass = `badge-${n.status === "complete" ? "complete" : n.status === "error" ? "error" : "pending"}`;

  card.innerHTML = `
    <span class="meeting-card-date">${esc(displayDate)}</span>
    <div class="meeting-card-body">
      <div class="meeting-card-title">${esc(n.title)}</div>
      <div class="meeting-card-sub">${esc(n.station || "TV News")}${n.air_time ? " \u00b7 " + esc(n.air_time) : ""}</div>
    </div>
    <span class="meeting-card-badge ${statusClass}">${esc(n.status)}</span>
    <button class="btn btn-ghost btn-sm delete-btn" title="Delete">\u2715</button>
  `;

  card.addEventListener("click", e => {
    if (e.target.closest(".delete-btn")) return;
    window.location.href = `/news/${n.newscast_id}`;
  });

  card.querySelector(".delete-btn").addEventListener("click", async e => {
    e.stopPropagation();
    if (!confirm(`Delete "${n.title}"? This cannot be undone.`)) return;
    const r = await fetch(`/api/news/${n.newscast_id}`, { method: "DELETE" });
    if (r.ok) {
      loadContent();
      loadRecent();
    } else {
      alert("Failed to delete newscast.");
    }
  });

  return card;
}

// ── Progress & Status ────────────────────────────────────────────────────────

function renderProgressBar(progressEl, status) {
  const pct = status.progress_pct ?? 0;
  const stage = status.stage || "";
  const detail = status.detail || "";
  const isComplete = status.status === "complete";
  const isError = status.status === "error";
  const indeterminate = !isComplete && !isError && pct === 0 && stage !== "";

  if (isError) {
    progressEl.innerHTML = `
      <div class="pipeline-progress pipeline-error">
        <div class="progress-header">
          <span class="progress-stage" style="color: var(--red-500, #ef4444)">Error: ${esc(stage)}</span>
        </div>
        ${detail ? `<div class="progress-detail" style="color: var(--red-400, #f87171)">${esc(detail)}</div>` : ""}
      </div>
    `;
    return;
  }

  progressEl.innerHTML = `
    <div class="pipeline-progress">
      <div class="progress-header">
        <span class="progress-stage">${esc(stage)}</span>
        <span class="progress-pct">${isComplete ? "\u2713 done" : pct ? pct + "%" : ""}</span>
      </div>
      <div class="progress-bar-track">
        <div class="progress-bar-fill${indeterminate ? " indeterminate" : ""}"
             style="width: ${indeterminate ? 40 : pct}%"></div>
      </div>
      ${detail ? `<div class="progress-detail">${esc(detail)}</div>` : ""}
    </div>
  `;
}

function applyStatusToBadge(badge, status, meetingId) {
  if (!badge) return;

  if (status.status === "error") {
    badge.textContent = "error";
    badge.className = "meeting-card-badge badge-error";
    showProcessBtn(meetingId);
  } else if (status.status === "complete" && status.segment_count > 0) {
    badge.textContent = `${status.segment_count} segs`;
    badge.className = "meeting-card-badge badge-complete";
    hideProcessBtn(meetingId);
  } else if (status.status === "processing") {
    badge.textContent = "processing";
    badge.className = "meeting-card-badge badge-processing";
    hideProcessBtn(meetingId);
  } else if (status.status === "pending" && status.segment_count === 0) {
    // Check if this meeting has media but no segments (unprocessed)
    checkUnprocessed(meetingId, badge);
  } else {
    badge.textContent = "no media";
    badge.className = "meeting-card-badge badge-pending";
  }
}

async function checkUnprocessed(meetingId, badge) {
  try {
    const r = await fetch(`/api/media/${meetingId}`);
    if (!r.ok) return;
    const files = await r.json();
    const hasMedia = files.some(f => f.file_type === "video" || f.file_type === "audio");
    if (hasMedia) {
      badge.textContent = "unprocessed";
      badge.className = "meeting-card-badge badge-unprocessed";
      showProcessBtn(meetingId);
    } else {
      badge.textContent = "no media";
      badge.className = "meeting-card-badge badge-pending";
    }
  } catch { /* ignore */ }
}

function showProcessBtn(meetingId) {
  const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
  if (!card) return;
  const btn = card.querySelector(".process-btn");
  if (btn) btn.style.display = "";
}

function hideProcessBtn(meetingId) {
  const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
  if (!card) return;
  const btn = card.querySelector(".process-btn");
  if (btn) btn.style.display = "none";
}

async function updateMeetingCardStatus(meetingId) {
  const badge = document.getElementById(`badge-${meetingId}`);
  const progressEl = document.getElementById(`progress-${meetingId}`);

  const status = await getMeetingStatus(meetingId);
  if (!status) {
    if (badge) {
      badge.textContent = "pending";
      badge.className = "meeting-card-badge badge-pending";
    }
    return;
  }

  applyStatusToBadge(badge, status, meetingId);

  if (status.status === "error" && progressEl) {
    renderProgressBar(progressEl, status);
    // Stop polling on error — user must retry manually
  } else if (status.status === "processing" && progressEl) {
    renderProgressBar(progressEl, status);
    startPolling(meetingId);
  } else if (status.status === "complete" && progressEl) {
    renderProgressBar(progressEl, status);
  }
}

function startPolling(meetingId) {
  if (activePollers[meetingId]) return;

  activePollers[meetingId] = setInterval(async () => {
    const badge = document.getElementById(`badge-${meetingId}`);
    const progressEl = document.getElementById(`progress-${meetingId}`);
    if (!badge && !progressEl) {
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
      return;
    }

    const status = await getMeetingStatus(meetingId);
    if (!status) {
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
      return;
    }

    applyStatusToBadge(badge, status, meetingId);
    if (progressEl) renderProgressBar(progressEl, status);

    if (status.status === "complete" || status.status === "error") {
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
    }
  }, POLL_INTERVAL);
}

// ── New Item Dialog ─────────────────────────────────────────────────────────

function openDialog() {
  selectedCategory = "meeting";
  document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
  document.querySelector('.cat-btn[data-cat="meeting"]').classList.add("active");
  updateDialogFields();

  document.getElementById("new-item-dialog").showModal();
  document.getElementById("overlay").classList.add("active");
}

function closeAllDialogs() {
  document.getElementById("new-item-dialog").close();
  document.getElementById("edit-dialog").close();
  document.getElementById("ingest-dialog").close();
  document.getElementById("overlay").classList.remove("active");
  if (ingestPoller) {
    clearInterval(ingestPoller);
    ingestPoller = null;
  }
}

function updateDialogFields() {
  const isMeeting = selectedCategory === "meeting";
  const isAudio = selectedCategory === "audio";
  const isNews = selectedCategory === "news";

  document.getElementById("meeting-fields").style.display = isMeeting ? "" : "none";
  document.getElementById("audio-fields").style.display = isAudio ? "" : "none";
  document.getElementById("news-fields").style.display = isNews ? "" : "none";

  const titles = { meeting: "New Meeting", audio: "New Audio Transcription", news: "New Newscast" };
  document.getElementById("dialog-title").textContent = titles[selectedCategory];

  const placeholders = {
    meeting: "e.g. January Regular Meeting",
    audio: "e.g. KQED Forum - Feb 19",
    news: "e.g. KRCR Evening News",
  };
  document.getElementById("f-title").placeholder = placeholders[selectedCategory];
}

async function handleCreate() {
  const title = document.getElementById("f-title").value.trim();
  if (!title) {
    alert("Please enter a title.");
    return;
  }

  const btn = document.getElementById("create-btn");
  btn.textContent = "Creating...";
  btn.disabled = true;

  try {
    if (selectedCategory === "news") {
      await createNewscast(title);
    } else {
      await createMeeting(title);
    }
    closeAllDialogs();
    loadContent();
    loadRecent();
  } catch (err) {
    alert(`Failed to create: ${err.message}`);
  } finally {
    btn.textContent = "Create";
    btn.disabled = false;
  }
}

async function createMeeting(title) {
  const isMeeting = selectedCategory === "meeting";
  const date = isMeeting
    ? document.getElementById("f-meeting-date").value
    : document.getElementById("f-audio-date").value;

  if (!date) throw new Error("Please select a date.");

  const gbId = isMeeting ? document.getElementById("f-governing-body").value : "";
  const gb = governingBodies.find(g => g.governing_body_id === gbId);

  const payload = {
    title,
    meeting_date: date,
    category: selectedCategory,
    governing_body: gb ? gb.name : "",
    governing_body_id: gbId || null,
    meeting_type: isMeeting ? document.getElementById("f-meeting-type").value : "",
  };

  const r = await fetch("/api/meetings/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail?.[0]?.msg || `HTTP ${r.status}`);
  }
  const meeting = await r.json();
  window.location.href = `/review/${meeting.meeting_id}`;
}

async function createNewscast(title) {
  const payload = {
    title,
    station: document.getElementById("f-station").value.trim(),
    air_date: document.getElementById("f-air-date").value || null,
    air_time: document.getElementById("f-air-time").value || null,
  };

  const r = await fetch("/api/news/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const newscast = await r.json();
  window.location.href = `/news/${newscast.newscast_id}`;
}

async function handleAddGoverningBody() {
  const input = document.getElementById("f-new-gb");
  const name = input.value.trim();
  if (!name) return;

  try {
    const r = await fetch("/api/governing-bodies/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, display_name: name }),
    });
    if (r.status === 409) {
      alert("That governing body already exists.");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);

    input.value = "";
    await loadGoverningBodies();

    const select = document.getElementById("f-governing-body");
    const newGb = governingBodies.find(g => g.name === name);
    if (newGb) select.value = newGb.governing_body_id;
  } catch (err) {
    alert(`Failed to add: ${err.message}`);
  }
}

// ── Edit Dialog ─────────────────────────────────────────────────────────────

function openEditDialog(meeting) {
  editMeetingId = meeting.meeting_id;
  document.getElementById("edit-title").value = meeting.title || "";
  document.getElementById("edit-description").value = meeting.description || "";
  document.getElementById("edit-date").value = meeting.meeting_date || "";

  document.getElementById("edit-dialog").showModal();
  document.getElementById("overlay").classList.add("active");
}

async function handleSaveEdit() {
  if (!editMeetingId) return;

  const payload = {
    title: document.getElementById("edit-title").value.trim() || undefined,
    description: document.getElementById("edit-description").value.trim() || undefined,
    meeting_date: document.getElementById("edit-date").value || undefined,
  };

  // Remove undefined fields
  Object.keys(payload).forEach(k => payload[k] === undefined && delete payload[k]);

  const btn = document.getElementById("save-edit-btn");
  btn.textContent = "Saving...";
  btn.disabled = true;

  try {
    const r = await fetch(`/api/meetings/${editMeetingId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    closeAllDialogs();
    loadContent();
    loadRecent();
  } catch (err) {
    alert(`Failed to save: ${err.message}`);
  } finally {
    btn.textContent = "Save";
    btn.disabled = false;
  }
}

// ── Ingest Dialog ───────────────────────────────────────────────────────────

async function openIngestDialog() {
  document.getElementById("ingest-dialog").showModal();
  document.getElementById("overlay").classList.add("active");
  document.getElementById("ingest-progress").style.display = "none";
  document.getElementById("run-ingest-btn").disabled = false;
  document.getElementById("run-ingest-btn").textContent = "Run All Sources";

  // Load sources
  const container = document.getElementById("ingest-sources");
  container.innerHTML = '<div class="loading-state-sm">Loading sources...</div>';

  try {
    const r = await fetch("/api/ingest/sources");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const sources = await r.json();

    if (sources.length === 0) {
      container.innerHTML = '<div class="loading-state-sm">No ingest sources configured. Run the migration first.</div>';
      return;
    }

    container.innerHTML = "";
    sources.forEach(src => {
      const lastScraped = src.last_scraped_at
        ? `Last run: ${new Date(src.last_scraped_at).toLocaleDateString()} (${src.last_scraped_count ?? 0} found)`
        : "Never run";

      const el = document.createElement("div");
      el.className = "ingest-source-item";
      el.innerHTML = `
        <div class="ingest-source-info">
          <div class="ingest-source-name">${esc(src.name)}</div>
          <div class="ingest-source-meta">${esc(src.source_type)} &middot; ${esc(lastScraped)}</div>
        </div>
        <button class="btn btn-ghost btn-sm" data-source-id="${src.source_id}">Run</button>
      `;

      el.querySelector("button").addEventListener("click", () => {
        runIngestForSource(src.source_id, src.name);
      });

      container.appendChild(el);
    });
  } catch (err) {
    container.innerHTML = `<div class="loading-state-sm">Failed to load sources: ${esc(err.message)}</div>`;
  }
}

async function handleRunIngest() {
  runIngestForSource(null, "all sources");
}

async function runIngestForSource(sourceId, label) {
  const btn = document.getElementById("run-ingest-btn");
  btn.disabled = true;
  btn.textContent = "Running...";

  const progressArea = document.getElementById("ingest-progress");
  progressArea.style.display = "";
  document.getElementById("ingest-stage").textContent = `Starting ingest for ${label}...`;
  document.getElementById("ingest-counts").textContent = "";

  try {
    const url = sourceId ? `/api/ingest/run?source_id=${sourceId}` : "/api/ingest/run";
    const r = await fetch(url, { method: "POST" });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }

    // Start polling for progress
    startIngestPolling();
  } catch (err) {
    document.getElementById("ingest-stage").textContent = `Error: ${err.message}`;
    btn.disabled = false;
    btn.textContent = "Run All Sources";
  }
}

function startIngestPolling() {
  if (ingestPoller) clearInterval(ingestPoller);

  ingestPoller = setInterval(async () => {
    try {
      const r = await fetch("/api/ingest/status");
      if (!r.ok) return;
      const status = await r.json();

      document.getElementById("ingest-stage").textContent = status.stage || (status.running ? "Running..." : "Complete");
      document.getElementById("ingest-counts").textContent =
        `Found: ${status.episodes_found || 0} | Downloaded: ${status.episodes_downloaded || 0}`;

      if (!status.running) {
        clearInterval(ingestPoller);
        ingestPoller = null;
        document.getElementById("run-ingest-btn").disabled = false;
        document.getElementById("run-ingest-btn").textContent = "Run All Sources";
        document.getElementById("ingest-bar").classList.remove("indeterminate");
        document.getElementById("ingest-bar").style.width = "100%";

        // Refresh the content grid
        loadContent();
        loadRecent();
      }
    } catch { /* ignore polling errors */ }
  }, 2000);
}

// ── Utilities ────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function formatDate(iso) {
  if (!iso || iso === "\u2014") return "\u2014";
  try {
    const [y, m, d] = iso.split("-");
    return new Date(+y, +m - 1, +d).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return iso; }
}

// ── Boot ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
