/**
 * clips.js — Clip Library page logic.
 * Polls in-progress exports, shows progress bars + ETA, and provides download buttons.
 */

"use strict";

const CLEANUP_HOURS = 24;  // must match app/config.py CLIP_CLEANUP_HOURS

let clips = [];
const activePollers = {};  // clipId -> { timer, firstPct, firstPctTime }
const sourceCache = {};    // sourceId -> { label, date }

async function init() {
  await loadClips();
}

async function loadClips() {
  try {
    const r = await fetch("/api/clips/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    clips = await r.json();
    await loadSourceInfo();
    renderClips();
  } catch (err) {
    console.error("Failed to load clips:", err);
    document.getElementById("clips-grid").innerHTML =
      '<div class="empty-state">Failed to load clips.</div>';
  }
}

async function loadSourceInfo() {
  // Collect unique source IDs by type
  const meetingIds = [...new Set(clips.filter(c => c.source_type === "meeting").map(c => c.source_id))];
  const newscastIds = [...new Set(clips.filter(c => c.source_type === "newscast").map(c => c.source_id))];

  // Fetch meetings in parallel
  const fetches = [];
  for (const id of meetingIds) {
    if (sourceCache[id]) continue;
    fetches.push(
      fetch(`/api/meetings/${id}`)
        .then(r => r.ok ? r.json() : null)
        .then(m => {
          if (!m) return;
          const date = m.meeting_date ? formatDate(m.meeting_date) : "";
          sourceCache[id] = {
            label: m.group_name || m.title || "Meeting",
            date,
          };
        })
        .catch(() => {})
    );
  }
  for (const id of newscastIds) {
    if (sourceCache[id]) continue;
    fetches.push(
      fetch(`/api/news/${id}`)
        .then(r => r.ok ? r.json() : null)
        .then(n => {
          if (!n) return;
          sourceCache[id] = {
            label: n.station || n.title || "Newscast",
            date: n.air_date ? formatDate(n.air_date) : "",
          };
        })
        .catch(() => {})
    );
  }
  await Promise.all(fetches);
}

function getSourceLine(clip) {
  const info = sourceCache[clip.source_id];
  if (info) {
    return info.date ? `${info.label} ${info.date}` : info.label;
  }
  return clip.source_type;
}

function renderClips() {
  const grid = document.getElementById("clips-grid");
  const countEl = document.getElementById("clip-count");

  // Stop any existing pollers
  Object.keys(activePollers).forEach(id => {
    clearInterval(activePollers[id].timer);
    delete activePollers[id];
  });

  countEl.textContent = `${clips.length} clip${clips.length !== 1 ? "s" : ""}`;

  if (clips.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <p>No clips yet.</p>
        <p class="empty-sub">Open a meeting or newscast and use the clip builder to create excerpts.</p>
      </div>`;
    return;
  }

  grid.innerHTML = "";
  clips.forEach(clip => {
    const card = document.createElement("div");
    card.className = "clip-card";
    card.id = `clip-card-${clip.clip_id}`;

    const thumbUrl = clip.thumbnail_path
      ? `/api/clips/${clip.clip_id}/thumbnail`
      : null;

    const countdown = getCountdown(clip);
    const sourceLine = getSourceLine(clip);

    card.innerHTML = `
      <div class="clip-card-thumb">
        ${thumbUrl
          ? `<img src="${thumbUrl}" alt="" class="clip-thumb-img">`
          : `<div class="clip-thumb-placeholder">&#9986;</div>`
        }
        <span class="clip-duration-badge">${fmtTime(clip.duration)}</span>
      </div>
      <div class="clip-card-body">
        <div class="clip-card-title">${esc(clip.title)}</div>
        <div class="clip-card-meta-row">
          <span class="clip-card-meta">${esc(sourceLine)}</span>
          <span class="clip-countdown" id="countdown-${clip.clip_id}">${countdown}</span>
        </div>
      </div>
      <div class="clip-card-actions">
        <span class="clip-primary-action" id="primary-${clip.clip_id}">${renderPrimaryAction(clip)}</span>
        <button class="btn btn-ghost btn-sm clip-action-btn" onclick="openSource('${clip.clip_id}')">Open Source</button>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm btn-danger clip-action-btn" onclick="deleteClip('${clip.clip_id}')">&#10005;</button>
      </div>
    `;

    grid.appendChild(card);

    // Start polling for in-progress exports
    if (clip.export_status === "exporting" || clip.export_status === "pending") {
      startPolling(clip.clip_id);
    }
  });
}

// ── Primary action: morphs between Re-export / progress bar / Download ──────

function renderPrimaryAction(clip) {
  const s = clip.export_status;

  if (s === "ready") {
    return `<a href="/api/clips/${clip.clip_id}/download" class="btn btn-primary btn-sm clip-download-btn">&#8615; Download</a>`;
  }

  if (s === "exporting" || s === "pending") {
    return `<span class="clip-card-badge clip-badge-exporting">exporting...</span>`;
  }

  if (s === "error") {
    return `<button class="btn btn-ghost btn-sm clip-action-btn" onclick="reExportClip('${clip.clip_id}')">&#8635; Re-export</button>`;
  }

  // cleaned or unknown
  return `<button class="btn btn-ghost btn-sm clip-action-btn" onclick="reExportClip('${clip.clip_id}')">&#8635; Re-export</button>`;
}

function getCountdown(clip) {
  if (!clip.created_at || clip.export_status !== "ready") return "";
  const created = new Date(clip.created_at).getTime();
  const expires = created + CLEANUP_HOURS * 3600 * 1000;
  const remaining = expires - Date.now();
  if (remaining <= 0) return "(download expiring soon)";

  const hours = remaining / (3600 * 1000);
  if (hours >= 48) {
    const days = Math.floor(hours / 24);
    return `(${days}d to download)`;
  }
  if (hours >= 1) {
    return `(${Math.floor(hours)}h to download)`;
  }
  const mins = Math.floor(remaining / 60000);
  return `(${mins}m to download)`;
}

// ── Polling for in-progress exports ─────────────────────────────────────────

function startPolling(clipId) {
  if (activePollers[clipId]) return;

  const state = { timer: null, firstPct: null, firstPctTime: null };

  state.timer = setInterval(async () => {
    try {
      const r = await fetch(`/api/clips/${clipId}/progress`);
      if (!r.ok) return;
      const data = await r.json();

      const primary = document.getElementById(`primary-${clipId}`);
      if (!primary) { stopPolling(clipId); return; }

      // Terminal states — stop polling and update the action area
      if (data.status === "ready" || data.status === "error" || data.status === "cleaned") {
        stopPolling(clipId);
        const clip = clips.find(c => c.clip_id === clipId);
        if (clip) {
          clip.export_status = data.status;
          if (data.error) clip.export_error = data.error;
          primary.innerHTML = renderPrimaryAction(clip);
          // Update countdown text
          const cdEl = document.getElementById(`countdown-${clipId}`);
          if (cdEl) cdEl.textContent = data.status === "ready" ? getCountdown(clip) : "";
        }
        return;
      }

      // Still exporting — show progress bar + ETA
      const pct = data.pct || 0;

      // Track first non-zero percentage for ETA calculation
      if (pct > 0 && state.firstPct === null) {
        state.firstPct = pct;
        state.firstPctTime = Date.now();
        if (data.started_at) {
          state.firstPctTime = data.started_at * 1000;
          state.firstPct = 0;
        }
      }

      let etaHtml = "";
      if (pct > 0 && state.firstPct !== null && pct > state.firstPct) {
        const elapsed = (Date.now() - state.firstPctTime) / 1000;
        const pctDone = pct - state.firstPct;
        const remaining = elapsed * (100 - pct) / pctDone;
        if (remaining > 0 && remaining < 3600) {
          const mins = Math.floor(remaining / 60);
          const secs = Math.floor(remaining % 60);
          etaHtml = `<span class="clip-eta">~${mins > 0 ? mins + "m " : ""}${secs}s remaining</span>`;
        }
      }

      primary.innerHTML = `
        <div class="clip-inline-progress">
          <div class="clip-progress-track">
            <div class="clip-progress-fill" style="width:${pct}%"></div>
          </div>
          <span class="clip-progress-pct">${pct}%</span>
          ${etaHtml}
        </div>`;

    } catch { /* ignore poll errors */ }
  }, 2000);

  activePollers[clipId] = state;
}

function stopPolling(clipId) {
  if (activePollers[clipId]) {
    clearInterval(activePollers[clipId].timer);
    delete activePollers[clipId];
  }
}

// ── Actions ─────────────────────────────────────────────────────────────────

async function deleteClip(clipId) {
  if (!confirm("Delete this clip?")) return;
  try {
    const r = await fetch(`/api/clips/${clipId}`, { method: "DELETE" });
    if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
    stopPolling(clipId);
    clips = clips.filter(c => c.clip_id !== clipId);
    renderClips();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

async function reExportClip(clipId) {
  try {
    const r = await fetch(`/api/clips/${clipId}/re-export`, { method: "POST" });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }
    const clip = clips.find(c => c.clip_id === clipId);
    if (clip) clip.export_status = "exporting";
    const primary = document.getElementById(`primary-${clipId}`);
    if (primary) {
      primary.innerHTML = `<span class="clip-card-badge clip-badge-exporting">exporting...</span>`;
    }
    startPolling(clipId);
  } catch (err) {
    alert(`Re-export failed: ${err.message}`);
  }
}

function openSource(clipId) {
  const clip = clips.find(c => c.clip_id === clipId);
  if (!clip) return;

  if (clip.source_type === "meeting") {
    window.location.href = `/review/${clip.source_id}?t=${clip.start_time}&clip=${clip.clip_id}`;
  } else if (clip.source_type === "newscast") {
    window.location.href = `/news/${clip.source_id}?t=${clip.start_time}&clip=${clip.clip_id}`;
  }
}

// ── Utilities ───────────────────────────────────────────────────────────────

function fmtTime(seconds) {
  if (seconds == null || isNaN(seconds)) return "0:00";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    const [y, m, d] = iso.split("-");
    return new Date(+y, +m - 1, +d).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return iso; }
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

document.addEventListener("DOMContentLoaded", init);
