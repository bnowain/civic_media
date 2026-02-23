/**
 * clips.js — Clip Library page logic.
 * Polls in-progress exports, shows progress bars + ETA, and provides download buttons.
 */

"use strict";

let clips = [];
const activePollers = {};  // clipId -> { timer, startedAt, firstPct }

async function init() {
  await loadClips();
}

async function loadClips() {
  try {
    const r = await fetch("/api/clips/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    clips = await r.json();
    renderClips();
  } catch (err) {
    console.error("Failed to load clips:", err);
    document.getElementById("clips-grid").innerHTML =
      '<div class="empty-state">Failed to load clips.</div>';
  }
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

    const dateStr = clip.created_at
      ? new Date(clip.created_at).toLocaleDateString("en-US", {
          year: "numeric", month: "short", day: "numeric",
        })
      : "";

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
        <div class="clip-card-meta">
          ${esc(clip.source_type)} &middot; ${dateStr}
        </div>
        <div class="clip-status-area" id="status-${clip.clip_id}">
          ${renderStatusBadge(clip)}
        </div>
      </div>
      <div class="clip-card-actions" id="actions-${clip.clip_id}">
        ${renderActions(clip)}
      </div>
    `;

    grid.appendChild(card);

    // Start polling for in-progress exports
    if (clip.export_status === "exporting" || clip.export_status === "pending") {
      startPolling(clip.clip_id);
    }
  });
}

function renderStatusBadge(clip) {
  if (clip.export_status === "ready") {
    return `<span class="clip-card-badge clip-badge-ready">ready</span>`;
  }
  if (clip.export_status === "error") {
    return `<span class="clip-card-badge clip-badge-error">error</span>`;
  }
  if (clip.export_status === "exporting" || clip.export_status === "pending") {
    return `<span class="clip-card-badge clip-badge-exporting">exporting</span>`;
  }
  return `<span class="clip-card-badge clip-badge-cleaned">${esc(clip.export_status || "unknown")}</span>`;
}

function renderActions(clip) {
  let html = "";
  if (clip.export_status === "ready") {
    html += `<a href="/api/clips/${clip.clip_id}/download" class="btn btn-ghost btn-sm clip-action-btn">&#8615; Download</a>`;
  }
  if (clip.export_status === "cleaned" || clip.export_status === "error") {
    html += `<button class="btn btn-ghost btn-sm clip-action-btn" onclick="reExportClip('${clip.clip_id}')">&#8635; Re-export</button>`;
  }
  html += `<button class="btn btn-ghost btn-sm clip-action-btn" onclick="openSource('${clip.clip_id}')">Open Source</button>`;
  html += `<button class="btn btn-ghost btn-sm btn-danger clip-action-btn" onclick="deleteClip('${clip.clip_id}')">&#10005;</button>`;
  return html;
}

// ── Polling for in-progress exports ─────────────────────────────────────────

function startPolling(clipId) {
  if (activePollers[clipId]) return;

  const state = { timer: null, pollStart: Date.now(), firstPct: null, firstPctTime: null };

  state.timer = setInterval(async () => {
    try {
      const r = await fetch(`/api/clips/${clipId}/progress`);
      if (!r.ok) return;
      const data = await r.json();

      const statusArea = document.getElementById(`status-${clipId}`);
      if (!statusArea) { stopPolling(clipId); return; }

      if (data.status === "ready") {
        stopPolling(clipId);
        updateClipLocal(clipId, "ready");
        statusArea.innerHTML = `<span class="clip-card-badge clip-badge-ready">ready</span>`;
        const actionsEl = document.getElementById(`actions-${clipId}`);
        if (actionsEl) {
          const clip = clips.find(c => c.clip_id === clipId);
          if (clip) actionsEl.innerHTML = renderActions(clip);
        }
      } else if (data.status === "error") {
        stopPolling(clipId);
        updateClipLocal(clipId, "error");
        statusArea.innerHTML = `<span class="clip-card-badge clip-badge-error">error: ${esc(data.error || "export failed")}</span>`;
        const actionsEl = document.getElementById(`actions-${clipId}`);
        if (actionsEl) {
          const clip = clips.find(c => c.clip_id === clipId);
          if (clip) actionsEl.innerHTML = renderActions(clip);
        }
      } else {
        // Exporting — show progress bar + ETA
        const pct = data.pct || 0;

        // Track first non-zero percentage for ETA calculation
        if (pct > 0 && state.firstPct === null) {
          state.firstPct = pct;
          state.firstPctTime = Date.now();
          // Use started_at from server if available
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

        statusArea.innerHTML = `
          <div class="clip-inline-progress">
            <div class="clip-progress-track">
              <div class="clip-progress-fill" style="width:${pct}%"></div>
            </div>
            <span class="clip-progress-pct">${pct}%</span>
            ${etaHtml}
          </div>`;
      }
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

function updateClipLocal(clipId, newStatus) {
  const clip = clips.find(c => c.clip_id === clipId);
  if (clip) clip.export_status = newStatus;
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
    // Update local state and start polling
    updateClipLocal(clipId, "exporting");
    const statusArea = document.getElementById(`status-${clipId}`);
    if (statusArea) {
      statusArea.innerHTML = `<span class="clip-card-badge clip-badge-exporting">exporting</span>`;
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

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

document.addEventListener("DOMContentLoaded", init);
