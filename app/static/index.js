/**
 * index.js — Library page logic.
 * Unified landing page with sidebar filters, tabs (Meetings/Audio/News),
 * recent media sidebar, and radio show ingest system.
 */

"use strict";

// ── State ────────────────────────────────────────────────────────────────────

let activeMode = "meetings";          // "meetings" | "audio" | "news" | "web_series"
let groupId = null;           // null = all
let recentSort = "processed";         // "processed" | "event"
let gridSort = "date";                // "date" | "group"
let selectedShow = null;              // for audio/web_series sidebar filter
let selectedCategory = "meeting";     // dialog category
let groups = [];             // cached list
let editMeetingId = null;             // meeting being edited

const activePollers = {};
const POLL_INTERVAL = 4000;
let ingestPoller = null;
let primegovPoller = null;

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  // Parse URL state
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab")) activeMode = params.get("tab");
  if (params.get("gb")) groupId = params.get("gb");
  if (params.get("sort")) recentSort = params.get("sort");
  if (params.get("gsort")) gridSort = params.get("gsort");

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

  // Sidebar labels depend on initial activeMode
  updateSidebarLabels();

  // Load data — sidebar is type-filtered per tab
  await Promise.all([loadGroups(), loadSidebarForTab(), loadRecent(), loadContent()]);

  // Event listeners — New Item dialog
  document.getElementById("new-item-btn").addEventListener("click", openDialog);
  document.getElementById("close-dialog-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-dialog-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("create-btn").addEventListener("click", handleCreate);
  document.getElementById("overlay").addEventListener("click", closeAllDialogs);
  document.getElementById("f-title").addEventListener("keydown", e => {
    if (e.key === "Enter") handleCreate();
  });
  document.getElementById("add-group-btn").addEventListener("click", handleAddGroup);

  // Edit dialog
  document.getElementById("close-edit-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-edit-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("save-edit-btn").addEventListener("click", handleSaveEdit);

  // Ingest dialog
  document.getElementById("close-ingest-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-ingest-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("run-ingest-btn").addEventListener("click", handleRunIngest);

  // PrimeGov dialog
  document.getElementById("close-primegov-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("cancel-primegov-btn").addEventListener("click", closeAllDialogs);
  document.getElementById("run-primegov-btn").addEventListener("click", handleRunPrimeGov);

  // Tabs
  document.getElementById("library-tabs").addEventListener("click", e => {
    const tab = e.target.closest(".lib-tab");
    if (!tab) return;
    activeMode = tab.dataset.mode;
    selectedShow = null;
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

    updateSidebarLabels();
    loadSidebarForTab();
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

  // Worker status pill
  document.getElementById("worker-status-btn").addEventListener("click", handleWorkerClick);
  pollWorkerHealth();
  setInterval(pollWorkerHealth, 10000);

  // Sidebar gb-list click — unified handler for meetings (governing body) and audio/web shows (show name)
  document.getElementById("gb-list").addEventListener("click", e => {
    const item = e.target.closest(".gb-item");
    if (!item) return;
    document.querySelectorAll("#gb-list .gb-item").forEach(i => i.classList.remove("active"));
    item.classList.add("active");
    if (activeMode === "meetings") {
      groupId = item.dataset.gbId || null;
      updateURL();
      loadContent();
    } else {
      selectedShow = item.dataset.show || null;
      loadContent();
    }
  });

  // Close sidebar on mobile when clicking sidebar items
  document.getElementById("sidebar").addEventListener("click", function(e) {
    if (e.target.closest(".gb-item") || e.target.closest(".recent-item") || e.target.closest(".station-list a")) {
      closeMobileSidebar();
    }
  });

  updateToolbar();
}

function closeMobileSidebar() {
  var sidebar = document.getElementById("sidebar");
  if (sidebar) sidebar.classList.remove("mobile-open");
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
  if (groupId) params.set("gb", groupId);
  if (recentSort !== "processed") params.set("sort", recentSort);
  if (gridSort !== "date") params.set("gsort", gridSort);
  const qs = params.toString();
  history.pushState(null, "", qs ? `/?${qs}` : "/");
}

function updateSidebarLabels() {
  const label = document.getElementById("gb-section-label");
  if (!label) return;
  const labels = {
    meetings:   "Governing Bodies",
    audio:      "Shows",
    web_series: "Shows",
    news:       "Governing Bodies",
  };
  label.textContent = labels[activeMode] || "Governing Bodies";
}

// ── Toolbar ──────────────────────────────────────────────────────────────────

function updateToolbar() {
  const actions = document.getElementById("toolbar-actions");
  if (!actions) return;

  // Action button (left side)
  let actionHtml = "";
  if (activeMode === "meetings") {
    actionHtml = `<button class="btn btn-amber btn-sm" id="primegov-btn">Discover BOS</button>`;
  } else if (activeMode === "audio") {
    actionHtml = `<button class="btn btn-amber btn-sm" id="ingest-btn">Ingest Radio Shows</button>`;
  }

  // Sort toggle (right side) — label changes per tab
  const groupLabels = {
    meetings:   "By Org",
    audio:      "By Show",
    news:       "By Program",
    web_series: "By Show",
  };
  const groupLabel = groupLabels[activeMode] || "By Group";
  const sortHtml = `
    <div class="grid-sort-toggle" id="grid-sort-toggle">
      <button class="toggle-btn${gridSort === "date" ? " active" : ""}" data-gsort="date">Date ↓</button>
      <button class="toggle-btn${gridSort === "group" ? " active" : ""}" data-gsort="group">${groupLabel}</button>
    </div>`;

  actions.innerHTML = actionHtml + sortHtml;

  if (activeMode === "meetings") {
    document.getElementById("primegov-btn").addEventListener("click", openPrimeGovDialog);
  } else if (activeMode === "audio") {
    document.getElementById("ingest-btn").addEventListener("click", openIngestDialog);
  }

  document.getElementById("grid-sort-toggle").addEventListener("click", e => {
    const btn = e.target.closest(".toggle-btn");
    if (!btn || !btn.dataset.gsort) return;
    gridSort = btn.dataset.gsort;
    document.querySelectorAll("#grid-sort-toggle .toggle-btn").forEach(b =>
      b.classList.toggle("active", b.dataset.gsort === gridSort));
    updateURL();
    loadContent();
  });
}

// ── API ──────────────────────────────────────────────────────────────────────

async function loadGroups() {
  try {
    const r = await fetch("/api/groups/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    groups = await r.json();
    renderGroups();
  } catch (err) {
    console.error("Failed to load governing bodies:", err);
  }
}

async function loadSidebarForTab() {
  const list = document.getElementById("gb-list");
  const type = (activeMode === "audio" || activeMode === "web_series") ? "show" : "government";
  try {
    const r = await fetch(`/api/groups/?group_type=${type}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const bodies = await r.json();
    if (activeMode === "audio" || activeMode === "web_series") {
      list.innerHTML = `<div class="gb-item${!selectedShow ? " active" : ""}" data-show="">All</div>`;
      bodies.forEach(gb => {
        const el = document.createElement("div");
        el.className = "gb-item" + (selectedShow === gb.name ? " active" : "");
        el.dataset.show = gb.name;
        el.textContent = gb.display_name || gb.name;
        list.appendChild(el);
      });
    } else {
      list.innerHTML = `<div class="gb-item${!groupId ? " active" : ""}" data-gb-id="">All</div>`;
      bodies.forEach(gb => {
        const el = document.createElement("div");
        el.className = "gb-item" + (groupId === gb.group_id ? " active" : "");
        el.dataset.gbId = gb.group_id;
        el.textContent = gb.display_name || gb.name;
        list.appendChild(el);
      });
    }
  } catch (err) {
    console.error("Failed to load sidebar for tab:", err);
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

// Group items by a key function and render with section headers
function renderGrouped(items, grid, keyFn, noKeyLabel = "Other") {
  const sorted = [...items].sort((a, b) => {
    const ka = keyFn(a) || noKeyLabel;
    const kb = keyFn(b) || noKeyLabel;
    if (ka < kb) return -1;
    if (ka > kb) return 1;
    return (b.meeting_date || "0000") < (a.meeting_date || "0000") ? -1 : 1;
  });

  const groups = new Map();
  for (const item of sorted) {
    const key = keyFn(item) || noKeyLabel;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }

  for (const [key, groupItems] of groups) {
    const header = document.createElement("div");
    header.className = "grid-group-header";
    header.textContent = key;
    grid.appendChild(header);
    groupItems.forEach(item => {
      const card = createMeetingCard(item);
      grid.appendChild(card);
    });
  }
}

// Populate the sidebar with unique show names derived from fetched items (audio/web_series tabs)
function updateShowSidebar(allItems) {
  const list = document.getElementById("gb-list");
  const shows = [...new Set(allItems.map(i => i.group_name).filter(Boolean))].sort();
  list.innerHTML = `<div class="gb-item${!selectedShow ? " active" : ""}" data-show="">All</div>`;
  shows.forEach(show => {
    const el = document.createElement("div");
    el.className = "gb-item" + (selectedShow === show ? " active" : "");
    el.dataset.show = show;
    el.textContent = show;
    list.appendChild(el);
  });
}

async function loadMeetingContent(grid) {
  const categoryMap = { meetings: "meeting", audio: "audio", web_series: "web_series" };
  const category = categoryMap[activeMode] || "meeting";
  let url = `/api/meetings/?category=${category}`;
  // Meetings tab uses server-side governing body filter; audio/web_series filter client-side by show
  if (activeMode === "meetings" && groupId) url += `&group_id=${groupId}`;

  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  let items = await r.json();

  // For audio/web shows: filter by selected show name
  if (activeMode === "audio" || activeMode === "web_series") {
    if (selectedShow) items = items.filter(i => i.group_name === selectedShow);
  }

  const labelMap = { meetings: "meeting", audio: "item", web_series: "video" };
  const label = labelMap[activeMode] || "item";
  document.getElementById("item-count").textContent =
    `${items.length} ${label}${items.length !== 1 ? "s" : ""}`;

  if (items.length === 0) {
    const emptyMap = {
      meetings:   "No meetings yet. Create your first meeting to get started.",
      audio:      "No audio transcriptions yet.",
      web_series: "No web show videos yet.",
    };
    grid.innerHTML = `<div class="empty-state">${emptyMap[activeMode] || "Nothing here yet."}</div>`;
    return;
  }

  grid.innerHTML = "";

  if (gridSort === "group") {
    renderGrouped(items, grid, m => m.group_name || null,
      activeMode === "meetings" ? "No Org" : "No Show");
  } else {
    items.forEach(m => {
      const card = createMeetingCard(m);
      grid.appendChild(card);
    });
  }

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

function renderGroups() {
  const list = document.getElementById("gb-list");
  list.innerHTML = '<div class="gb-item active" data-gb-id="">All</div>';

  groups.forEach(gb => {
    const el = document.createElement("div");
    el.className = "gb-item";
    el.dataset.gbId = gb.group_id;
    el.textContent = gb.display_name || gb.name;
    if (groupId === gb.group_id) {
      el.classList.add("active");
      list.querySelector('[data-gb-id=""]').classList.remove("active");
    }
    list.appendChild(el);
  });

  // Also populate the dialog dropdown
  const select = document.getElementById("f-group");
  select.innerHTML = '<option value="">Select...</option>';
  groups.forEach(gb => {
    const opt = document.createElement("option");
    opt.value = gb.group_id;
    opt.textContent = gb.display_name || gb.name;
    select.appendChild(opt);
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
  if (m.primegov_id) card.dataset.primegovId = m.primegov_id;

  const displayDate = formatDate(m.meeting_date || "\u2014");
  const isAudio = m.category === "audio";

  const subLine = isAudio
    ? (m.group_name ? esc(m.group_name) : "Audio Transcription")
    : `${esc(m.group_name)} \u00b7 ${esc(m.meeting_type)}`;

  const descLine = m.description
    ? `<div class="meeting-card-desc">${esc(m.description)}</div>` : "";

  // Thumbnail for ingested audio
  const thumbHtml = m.thumbnail_url
    ? `<img class="meeting-card-thumb" src="${esc(m.thumbnail_url)}" alt="" loading="lazy">`
    : "";

  // Asset icons for PrimeGov meetings
  const assetHtml = m.primegov_id ? `<div class="asset-icons" id="assets-${m.meeting_id}"></div>` : "";

  // Source provenance link
  let sourceLinkHtml = "";
  if (m.page_url) {
    sourceLinkHtml = `<a class="meeting-card-source" href="${esc(m.page_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Source \u2197</a>`;
  } else if (m.video_url) {
    sourceLinkHtml = `<a class="meeting-card-source" href="${esc(m.video_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Source \u2197</a>`;
  } else if (m.source_url) {
    sourceLinkHtml = `<a class="meeting-card-source" href="${esc(m.source_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Source \u2197</a>`;
  }

  card.innerHTML = `
    ${thumbHtml}
    <span class="meeting-card-date">${esc(displayDate)}</span>
    <div class="meeting-card-body">
      <div class="meeting-card-title">${esc(m.title)}</div>
      <div class="meeting-card-sub">${subLine}</div>
      ${descLine}
      ${assetHtml}
      ${sourceLinkHtml}
      <div class="meeting-progress" id="progress-${m.meeting_id}"></div>
    </div>
    <span class="meeting-card-badge badge-pending" id="badge-${m.meeting_id}">\u2014</span>
    <div class="meeting-card-actions">
      <button class="btn btn-ghost btn-sm edit-btn" data-meeting-id="${m.meeting_id}" title="Edit">&#x270E;</button>
      <button class="btn btn-amber btn-sm download-btn" data-meeting-id="${m.meeting_id}" title="Download from PrimeGov" style="display:none">&#x2B07;</button>
      <button class="btn btn-amber btn-sm transcode-btn" data-meeting-id="${m.meeting_id}" title="Transcode to 540p" style="display:none">540p</button>
      <button class="btn btn-ghost btn-sm process-btn" data-meeting-id="${m.meeting_id}" title="Process" style="display:none">&#x25B6;</button>
      <button class="btn btn-ghost btn-sm delete-btn" data-meeting-id="${m.meeting_id}" title="Delete">&#x2715;</button>
    </div>
  `;

  // Store meeting data on the card for edit dialog
  card._meetingData = m;

  // Fetch PrimeGov asset status
  if (m.primegov_id) {
    fetchAssetStatus(m.meeting_id);
  }

  card.addEventListener("click", e => {
    if (e.target.closest(".delete-btn") || e.target.closest(".edit-btn") || e.target.closest(".process-btn") || e.target.closest(".download-btn") || e.target.closest(".transcode-btn") || e.target.closest(".meeting-card-source")) return;
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

  card.querySelector(".download-btn").addEventListener("click", async e => {
    e.stopPropagation();
    const btn = e.target.closest(".download-btn");
    btn.disabled = true;
    btn.textContent = "...";
    try {
      const r = await fetch(`/api/primegov/download/${m.meeting_id}`, { method: "POST" });
      if (r.ok) {
        const result = await r.json();

        // Report document download results
        const docs = result.documents || {};
        const docErrors = Object.entries(docs)
          .filter(([, v]) => v.status === "error")
          .map(([k, v]) => `${k}: ${v.error}`);
        const docOk = Object.entries(docs)
          .filter(([, v]) => v.status === "complete")
          .map(([k]) => k);

        // Refresh asset icons for any docs that downloaded
        if (docOk.length > 0) fetchAssetStatus(m.meeting_id);

        if (result.task_id) {
          // Video download queued in Celery — poll for progress
          btn.textContent = "\u23F3";
          const badge = document.getElementById(`badge-${m.meeting_id}`);
          if (badge) {
            badge.textContent = "downloading video";
            badge.className = "meeting-card-badge badge-processing";
          }
          startPolling(m.meeting_id);
        } else {
          // No video task — docs-only download complete
          btn.style.display = "none";
          const messages = [];
          if (result.video_note) messages.push(result.video_note);
          if (docErrors.length > 0) messages.push(...docErrors);

          if (docOk.length > 0) {
            const badge = document.getElementById(`badge-${m.meeting_id}`);
            if (badge) {
              badge.textContent = `${docOk.length} doc${docOk.length > 1 ? "s" : ""} downloaded`;
              badge.className = "meeting-card-badge badge-unprocessed";
            }
          }
          if (messages.length > 0) {
            alert(messages.join("\n"));
          }
        }
      } else {
        const err = await r.json().catch(() => ({}));
        alert(err.detail || "Failed to start download.");
        btn.textContent = "\u2B07";
        btn.disabled = false;
      }
    } catch {
      btn.textContent = "\u2B07";
      btn.disabled = false;
    }
  });

  card.querySelector(".transcode-btn").addEventListener("click", async e => {
    e.stopPropagation();
    if (!confirm(`Transcode "${m.title}" to 540p? The original file will be deleted.`)) return;
    const btn = e.target.closest(".transcode-btn");
    btn.disabled = true;
    btn.textContent = "...";
    try {
      const r = await fetch(`/api/media/${m.meeting_id}/transcode`, { method: "POST" });
      if (r.ok) {
        btn.style.display = "none";
        const badge = document.getElementById(`badge-${m.meeting_id}`);
        if (badge) {
          badge.textContent = "transcoding";
          badge.className = "meeting-card-badge badge-processing";
        }
        startPolling(m.meeting_id);
      } else {
        const err = await r.json().catch(() => ({}));
        alert(err.detail || "Failed to start transcode.");
        btn.textContent = "540p";
        btn.disabled = false;
      }
    } catch {
      btn.textContent = "540p";
      btn.disabled = false;
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

  // Handle transcode states
  if (status.transcode_status === "transcoding") {
    badge.textContent = "transcoding";
    badge.className = "meeting-card-badge badge-processing";
    hideProcessBtn(meetingId);
    hideTranscodeBtn(meetingId);
    return;
  }

  if (status.status === "error") {
    badge.textContent = "error";
    badge.className = "meeting-card-badge badge-error";
    // Show the right retry button depending on where the failure occurred
    if (status.transcode_status === null || status.transcode_status === undefined) {
      // No media file exists — download failed, let user retry
      showDownloadBtn(meetingId);
      hideTranscodeBtn(meetingId);
      hideProcessBtn(meetingId);
    } else if (status.transcode_status === "pending") {
      showTranscodeBtn(meetingId);
      hideDownloadBtn(meetingId);
    } else {
      showProcessBtn(meetingId);
      hideDownloadBtn(meetingId);
    }
  } else if (status.status === "complete" && status.segment_count > 0) {
    badge.textContent = `${status.segment_count} segs`;
    badge.className = "meeting-card-badge badge-complete";
    hideProcessBtn(meetingId);
    hideTranscodeBtn(meetingId);
  } else if (status.status === "processing") {
    badge.textContent = "processing";
    badge.className = "meeting-card-badge badge-processing";
    hideProcessBtn(meetingId);
    hideTranscodeBtn(meetingId);
  } else if (status.status === "pending" && status.segment_count === 0) {
    // Check if this meeting has media but no segments (unprocessed)
    // For PrimeGov meetings without media, show "download" instead of "no media"
    checkUnprocessed(meetingId, badge, status.transcode_status);
  } else {
    // Check if this is a PrimeGov meeting before showing "no media"
    const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
    if (card && card.dataset.primegovId) {
      // Let fetchAssetStatus handle the badge for PrimeGov meetings
      if (!badge.textContent || badge.textContent === "\u2014") {
        badge.textContent = "pending";
        badge.className = "meeting-card-badge badge-pending";
      }
    } else {
      badge.textContent = "no media";
      badge.className = "meeting-card-badge badge-pending";
    }
  }
}

async function checkUnprocessed(meetingId, badge, transcodeStatus) {
  try {
    const r = await fetch(`/api/media/${meetingId}`);
    if (!r.ok) return;
    const files = await r.json();
    const sourceMedia = files.find(f =>
      (f.file_type === "video" || f.file_type === "audio") &&
      !f.file_path.includes("_extracted.wav")
    );
    if (sourceMedia) {
      const ts = transcodeStatus || sourceMedia.transcode_status;
      if (ts === "pending") {
        badge.textContent = "needs transcode";
        badge.className = "meeting-card-badge badge-downloadable";
        showTranscodeBtn(meetingId);
        hideProcessBtn(meetingId);
      } else if (ts === "transcoding") {
        badge.textContent = "transcoding";
        badge.className = "meeting-card-badge badge-processing";
        hideTranscodeBtn(meetingId);
        hideProcessBtn(meetingId);
        startPolling(meetingId);
      } else {
        badge.textContent = "unprocessed";
        badge.className = "meeting-card-badge badge-unprocessed";
        showProcessBtn(meetingId);
        hideTranscodeBtn(meetingId);
      }
    } else {
      // No media file — check if this is a PrimeGov meeting with downloadable assets
      const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
      if (card && card.dataset.primegovId) {
        badge.textContent = "download";
        badge.className = "meeting-card-badge badge-downloadable";
        const dlBtn = card.querySelector(".download-btn");
        if (dlBtn) dlBtn.style.display = "";
        hideProcessBtn(meetingId);
        hideTranscodeBtn(meetingId);
      } else {
        badge.textContent = "no media";
        badge.className = "meeting-card-badge badge-pending";
      }
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

function showTranscodeBtn(meetingId) {
  const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
  if (!card) return;
  const btn = card.querySelector(".transcode-btn");
  if (btn) btn.style.display = "";
}

function hideTranscodeBtn(meetingId) {
  const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
  if (!card) return;
  const btn = card.querySelector(".transcode-btn");
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

  if (progressEl) {
    const hasActiveStage = status.stage && status.stage !== "Waiting for upload";
    if (status.status === "error") {
      renderProgressBar(progressEl, status);
    } else if (status.status === "processing" || status.transcode_status === "transcoding") {
      renderProgressBar(progressEl, status);
      startPolling(meetingId);
    } else if (status.status === "complete") {
      renderProgressBar(progressEl, status);
    } else if (hasActiveStage) {
      // Show progress for downloads and other pending operations
      renderProgressBar(progressEl, status);
      startPolling(meetingId);
    }
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

    // Detect download completion: media now exists, needs transcode
    if (status.transcode_status === "pending" && status.status === "pending"
        && status.stage === "Ready to process") {
      badge.textContent = "needs transcode";
      badge.className = "meeting-card-badge badge-downloadable";
      hideDownloadBtn(meetingId);
      showTranscodeBtn(meetingId);
      hideProcessBtn(meetingId);
      if (progressEl) progressEl.innerHTML = "";
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
      return;
    }

    // Detect transcode completion: ready for pipeline processing
    if (status.transcode_status === "transcoded" && status.status === "pending") {
      badge.textContent = "unprocessed";
      badge.className = "meeting-card-badge badge-unprocessed";
      hideTranscodeBtn(meetingId);
      hideDownloadBtn(meetingId);
      showProcessBtn(meetingId);
      if (progressEl) progressEl.innerHTML = "";
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
      return;
    }

    if (status.status === "complete" || status.status === "error") {
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
    }
  }, POLL_INTERVAL);
}

function showDownloadBtn(meetingId) {
  const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
  if (!card) return;
  const btn = card.querySelector(".download-btn");
  if (btn) {
    btn.style.display = "";
    btn.disabled = false;
    btn.textContent = "\u2B07";
  }
}

function hideDownloadBtn(meetingId) {
  const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
  if (!card) return;
  const btn = card.querySelector(".download-btn");
  if (btn) btn.style.display = "none";
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
  document.getElementById("primegov-dialog").close();
  document.getElementById("overlay").classList.remove("active");
  if (ingestPoller) {
    clearInterval(ingestPoller);
    ingestPoller = null;
  }
  if (primegovPoller) {
    clearInterval(primegovPoller);
    primegovPoller = null;
  }
}

function updateDialogFields() {
  const isMeeting   = selectedCategory === "meeting";
  const isAudio     = selectedCategory === "audio";
  const isNews      = selectedCategory === "news";
  const isWebSeries = selectedCategory === "web_series";

  document.getElementById("meeting-fields").style.display = (isMeeting || isWebSeries) ? "" : "none";
  // For web_series: show governing body + date but hide meeting type row
  if (isWebSeries) {
    const mtRow = document.getElementById("f-meeting-type");
    if (mtRow) mtRow.closest(".form-row").style.display = "none";
  } else if (isMeeting) {
    const mtRow = document.getElementById("f-meeting-type");
    if (mtRow) mtRow.closest(".form-row").style.display = "";
  }
  document.getElementById("audio-fields").style.display = isAudio ? "" : "none";
  document.getElementById("news-fields").style.display = isNews ? "" : "none";

  const titles = {
    meeting:    "New Meeting",
    audio:      "New Audio Transcription",
    news:       "New Newscast",
    web_series: "New Web Show Video",
  };
  document.getElementById("dialog-title").textContent = titles[selectedCategory] || "New Item";

  const placeholders = {
    meeting:    "e.g. January Regular Meeting",
    audio:      "e.g. KQED Forum - Feb 19",
    news:       "e.g. KRCR Evening News",
    web_series: "e.g. Episode 42 — Special Coverage",
  };
  document.getElementById("f-title").placeholder = placeholders[selectedCategory] || "Title";
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
  const isMeeting   = selectedCategory === "meeting";
  const isWebSeries = selectedCategory === "web_series";
  const date = (isMeeting || isWebSeries)
    ? document.getElementById("f-meeting-date").value
    : document.getElementById("f-audio-date").value;

  if (!date) throw new Error("Please select a date.");

  const gbId = (isMeeting || isWebSeries) ? document.getElementById("f-group").value : "";
  const gb = groups.find(g => g.group_id === gbId);

  const payload = {
    title,
    meeting_date: date,
    category: selectedCategory,
    group_name: gb ? gb.name : "",
    group_id: gbId || null,
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

async function handleAddGroup() {
  const input = document.getElementById("f-new-group");
  const name = input.value.trim();
  if (!name) return;

  const bodyType = (selectedCategory === "audio" || selectedCategory === "web_series") ? "show" : "government";

  try {
    const r = await fetch("/api/groups/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, display_name: name, group_type: bodyType }),
    });
    if (r.status === 409) {
      alert("That group already exists.");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);

    input.value = "";
    await loadGroups();

    const select = document.getElementById("f-group");
    const newGb = groups.find(g => g.name === name);
    if (newGb) select.value = newGb.group_id;
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

// ── PrimeGov Discovery ──────────────────────────────────────────────────────

async function openPrimeGovDialog() {
  document.getElementById("primegov-dialog").showModal();
  document.getElementById("overlay").classList.add("active");
  document.getElementById("primegov-progress").style.display = "none";
  document.getElementById("run-primegov-btn").disabled = false;
  document.getElementById("run-primegov-btn").textContent = "Discover Meetings";

  // Load committees
  const container = document.getElementById("primegov-committees");
  container.innerHTML = '<div class="loading-state-sm">Loading committees...</div>';

  try {
    const r = await fetch("/api/primegov/committees");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const committees = await r.json();

    container.innerHTML = "";
    committees.forEach(c => {
      const label = document.createElement("label");
      label.className = "primegov-committee-item";
      const checked = c.id === 3 ? "checked" : "";
      label.innerHTML = `
        <input type="checkbox" value="${c.id}" ${checked}>
        <span>${esc(c.name)}</span>
      `;
      container.appendChild(label);
    });
  } catch (err) {
    container.innerHTML = `<div class="loading-state-sm">Failed to load: ${esc(err.message)}</div>`;
  }
}

async function handleRunPrimeGov() {
  const btn = document.getElementById("run-primegov-btn");
  btn.disabled = true;
  btn.textContent = "Discovering...";

  const progressArea = document.getElementById("primegov-progress");
  progressArea.style.display = "";
  document.getElementById("primegov-stage").textContent = "Starting discovery...";
  document.getElementById("primegov-detail").textContent = "";

  // Gather selected committee IDs
  const checkboxes = document.querySelectorAll("#primegov-committees input[type=checkbox]:checked");
  const ids = Array.from(checkboxes).map(cb => parseInt(cb.value));

  if (ids.length === 0) {
    alert("Please select at least one committee.");
    btn.disabled = false;
    btn.textContent = "Discover Meetings";
    progressArea.style.display = "none";
    return;
  }

  try {
    const params = ids.map(id => `committee_ids=${id}`).join("&");
    const r = await fetch(`/api/primegov/discover?${params}`, { method: "POST" });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }

    startPrimeGovPolling();
  } catch (err) {
    document.getElementById("primegov-stage").textContent = `Error: ${err.message}`;
    btn.disabled = false;
    btn.textContent = "Discover Meetings";
  }
}

function startPrimeGovPolling() {
  if (primegovPoller) clearInterval(primegovPoller);

  primegovPoller = setInterval(async () => {
    try {
      const r = await fetch("/api/primegov/discover/status");
      if (!r.ok) return;
      const status = await r.json();

      document.getElementById("primegov-stage").textContent = status.stage || "Working...";
      document.getElementById("primegov-detail").textContent = status.detail || "";

      const bar = document.getElementById("primegov-bar");
      if (status.pct > 0) {
        bar.classList.remove("indeterminate");
        bar.style.width = status.pct + "%";
      }

      if (status.pct >= 100 || status.error) {
        clearInterval(primegovPoller);
        primegovPoller = null;
        document.getElementById("run-primegov-btn").disabled = false;
        document.getElementById("run-primegov-btn").textContent = "Discover Meetings";

        if (status.error) {
          document.getElementById("primegov-stage").style.color = "var(--accent-red)";
        }

        // Refresh content
        loadContent();
        loadRecent();
      }
    } catch { /* ignore */ }
  }, 2000);
}

async function fetchAssetStatus(meetingId) {
  try {
    const r = await fetch(`/api/primegov/assets/${meetingId}`);
    if (!r.ok) return;
    const assets = await r.json();

    const assetsEl = document.getElementById(`assets-${meetingId}`);
    if (assetsEl) {
      const icons = [];
      if (assets.video_url_available) {
        icons.push(assets.video_downloaded
          ? '<span class="asset-icon asset-downloaded" title="Video downloaded">&#x1F3AC;</span>'
          : '<span class="asset-icon asset-available" title="Video available">&#x1F4F9;</span>'
        );
      }
      if (assets.agenda_url_available) {
        icons.push(assets.agenda_downloaded
          ? '<span class="asset-icon asset-downloaded" title="Agenda Overview downloaded">&#x1F4CB;</span>'
          : '<span class="asset-icon asset-available" title="Agenda Overview available">&#x1F4C4;</span>'
        );
      }
      if (assets.packet_url_available) {
        icons.push(assets.packet_downloaded
          ? '<span class="asset-icon asset-downloaded" title="Agenda Packet downloaded">&#x1F4E6;</span>'
          : '<span class="asset-icon asset-available" title="Agenda Packet available">&#x1F4E6;</span>'
        );
      }
      if (assets.minutes_url_available) {
        icons.push(assets.minutes_downloaded
          ? '<span class="asset-icon asset-downloaded" title="Minutes downloaded">&#x1F4DD;</span>'
          : '<span class="asset-icon asset-available" title="Minutes available">&#x1F4C3;</span>'
        );
      }
      assetsEl.innerHTML = icons.join(" ");
    }

    // Show download button if any asset is available but not downloaded
    const hasUndownloaded =
      (assets.video_url_available && !assets.video_downloaded) ||
      (assets.agenda_url_available && !assets.agenda_downloaded) ||
      (assets.packet_url_available && !assets.packet_downloaded) ||
      (assets.minutes_url_available && !assets.minutes_downloaded);

    if (hasUndownloaded) {
      const card = document.querySelector(`[data-meeting-id="${meetingId}"]`);
      if (card) {
        const dlBtn = card.querySelector(".download-btn");
        if (dlBtn) dlBtn.style.display = "";
      }

      // Set badge to "download" — override stale "no media" or initial "—"
      const badge = document.getElementById(`badge-${meetingId}`);
      if (badge) {
        const bt = badge.textContent;
        // Don't override active states like processing/transcoding/complete
        const activeStates = ["processing", "transcoding", "complete", "error", "unprocessed", "needs transcode"];
        if (!activeStates.includes(bt) && !bt.includes("segs")) {
          badge.textContent = "download";
          badge.className = "meeting-card-badge badge-downloadable";
        }
      }
    }
  } catch { /* ignore */ }
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

// ── Worker Health ─────────────────────────────────────────────────────────────

let workerState = "unknown";  // "online" | "offline" | "restarting" | "unknown"

async function pollWorkerHealth() {
  const dot = document.getElementById("worker-dot");
  const label = document.getElementById("worker-label");
  const btn = document.getElementById("worker-status-btn");
  if (!dot || !label || !btn) return;

  if (workerState === "restarting") return;  // don't poll during restart

  try {
    const r = await fetch("/api/system/worker-health");
    const data = await r.json();

    // Worker is "effectively running" if Celery ping succeeded, OR if the
    // watchdog process is alive (ping can fail when Whisper/GPU work blocks
    // the Celery main loop), OR if a job is actively writing progress.
    const effectivelyRunning = data.worker_online || data.watchdog_alive || !!data.active_job;

    if (effectivelyRunning) {
      workerState = "online";
      const job = data.active_job;
      if (job) {
        dot.className = "worker-dot processing";
        // Format date as "Feb 25" if available
        let dateStr = "";
        if (job.meeting_date) {
          try {
            const d = new Date(job.meeting_date + "T12:00:00");
            dateStr = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
          } catch { dateStr = job.meeting_date; }
        }
        const title = job.title || "";
        const shortTitle = title.length > 22 ? title.slice(0, 21) + "…" : title;
        const meta = [dateStr, shortTitle].filter(Boolean).join(" · ");
        label.textContent = `${job.stage} ${job.pct}%${meta ? " · " + meta : ""}`;
        btn.title = `${job.stage} (${job.pct}%) — ${dateStr ? dateStr + " " : ""}${title || "unknown meeting"}`;
      } else {
        dot.className = "worker-dot online";
        label.textContent = "Worker";
        btn.title = data.active_tasks
          ? `Worker online — ${data.active_tasks} active task(s)`
          : "Worker online";
      }
    } else {
      workerState = "offline";
      dot.className = "worker-dot offline";
      label.textContent = "Offline";
      btn.title = "Worker offline — click to restart";
    }
  } catch {
    workerState = "offline";
    dot.className = "worker-dot offline";
    label.textContent = "Offline";
    btn.title = "Worker offline — click to restart";
  }
}

async function handleWorkerClick() {
  if (workerState === "online" || workerState === "restarting") return;

  const dot = document.getElementById("worker-dot");
  const label = document.getElementById("worker-label");
  const btn = document.getElementById("worker-status-btn");

  // Set restarting state
  workerState = "restarting";
  dot.className = "worker-dot restarting";
  label.textContent = "Restarting...";
  btn.title = "Restarting worker...";

  try {
    const r = await fetch("/api/system/restart-worker", { method: "POST" });
    const data = await r.json();
    if (data.worker_online) {
      workerState = "online";
      dot.className = "worker-dot online";
      label.textContent = "Worker";
      btn.title = "Worker online";
    } else {
      // Worker launched but not yet responding — poll fast
      let attempts = 0;
      const fastPoll = setInterval(async () => {
        attempts++;
        try {
          const pr = await fetch("/api/system/worker-health");
          const pd = await pr.json();
          if (pd.worker_online) {
            clearInterval(fastPoll);
            workerState = "online";
            dot.className = "worker-dot online";
            label.textContent = "Worker";
            btn.title = "Worker online";
          }
        } catch { /* keep polling */ }
        if (attempts >= 10) {
          clearInterval(fastPoll);
          workerState = "offline";
          dot.className = "worker-dot offline";
          label.textContent = "Offline";
          btn.title = "Worker failed to restart — click to retry";
        }
      }, 2000);
    }
  } catch {
    workerState = "offline";
    dot.className = "worker-dot offline";
    label.textContent = "Offline";
    btn.title = "Restart failed — click to retry";
  }
}

// ── Boot ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
