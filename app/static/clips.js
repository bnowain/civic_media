/**
 * clips.js — Clip Library page logic.
 */

"use strict";

let clips = [];

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

    const statusClass = clip.export_status === "ready" ? "clip-badge-ready"
      : clip.export_status === "exporting" ? "clip-badge-exporting"
      : clip.export_status === "error" ? "clip-badge-error"
      : "clip-badge-cleaned";

    const statusText = clip.export_status || "unknown";

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
        <span class="clip-card-badge ${statusClass}">${esc(statusText)}</span>
      </div>
      <div class="clip-card-actions">
        ${clip.export_status === "ready"
          ? `<a href="/api/clips/${clip.clip_id}/download" class="btn btn-ghost btn-sm clip-action-btn">&#8615; Download</a>`
          : ""
        }
        ${clip.export_status === "cleaned"
          ? `<button class="btn btn-ghost btn-sm clip-action-btn" onclick="reExportClip('${clip.clip_id}')">&#8635; Re-export</button>`
          : ""
        }
        <button class="btn btn-ghost btn-sm clip-action-btn" onclick="openSource('${clip.clip_id}')">Open Source</button>
        <button class="btn btn-ghost btn-sm btn-danger clip-action-btn" onclick="deleteClip('${clip.clip_id}')">&#10005;</button>
      </div>
    `;

    grid.appendChild(card);
  });
}

async function deleteClip(clipId) {
  if (!confirm("Delete this clip?")) return;
  try {
    const r = await fetch(`/api/clips/${clipId}`, { method: "DELETE" });
    if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
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
    // Reload to show updated status
    await loadClips();
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
