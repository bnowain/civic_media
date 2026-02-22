/**
 * news_review.js — Newscast detail page logic.
 * Video player + story segments list with tags and mentions.
 */

"use strict";

// ── State ────────────────────────────────────────────────────────────────────

const newscastId = window.location.pathname.split("/").pop();
let newscast = null;
let segments = [];
let pollerId = null;
const POLL_INTERVAL = 4000;

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  await loadNewscast();

  // Upload handlers
  const uploadZone = document.getElementById("upload-zone");
  const fileInput = document.getElementById("file-input");

  uploadZone.addEventListener("click", () => fileInput.click());
  uploadZone.addEventListener("dragover", e => {
    e.preventDefault();
    uploadZone.classList.add("dragover");
  });
  uploadZone.addEventListener("dragleave", () => {
    uploadZone.classList.remove("dragover");
  });
  uploadZone.addEventListener("drop", e => {
    e.preventDefault();
    uploadZone.classList.remove("dragover");
    if (e.dataTransfer.files.length > 0) {
      uploadFile(e.dataTransfer.files[0]);
    }
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length > 0) {
      uploadFile(fileInput.files[0]);
    }
  });
}

// ── API ──────────────────────────────────────────────────────────────────────

async function loadNewscast() {
  try {
    const r = await fetch(`/api/news/${newscastId}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    newscast = await r.json();
    renderInfo();

    if (newscast.source_file) {
      showVideo();
    }

    if (["stripping", "transcribing", "tagging", "queued"].includes(newscast.status)) {
      showProgress();
      startPolling();
    }

    if (newscast.status === "complete") {
      await loadSegments();
    }
  } catch (err) {
    console.error("Failed to load newscast:", err);
    document.getElementById("news-title").textContent = "Newscast not found";
  }
}

async function loadSegments() {
  try {
    const r = await fetch(`/api/news/${newscastId}/segments`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    segments = await r.json();
    renderSegments();
  } catch (err) {
    console.error("Failed to load segments:", err);
  }
}

async function uploadFile(file) {
  const skipCommercials = document.getElementById("skip-commercials").checked;

  document.getElementById("upload-area").style.display = "none";
  showProgress();

  const formData = new FormData();
  formData.append("file", file);

  try {
    const url = `/api/news/${newscastId}/upload?skip_commercial_strip=${skipCommercials}`;
    const r = await fetch(url, { method: "POST", body: formData });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);

    startPolling();
  } catch (err) {
    console.error("Upload failed:", err);
    alert(`Upload failed: ${err.message}`);
    document.getElementById("upload-area").style.display = "";
    document.getElementById("progress-area").style.display = "none";
  }
}

// ── Render ───────────────────────────────────────────────────────────────────

function renderInfo() {
  document.getElementById("news-title").textContent = newscast.title;

  const parts = [];
  if (newscast.station) parts.push(newscast.station);
  if (newscast.air_date) parts.push(formatDate(newscast.air_date));
  if (newscast.air_time) parts.push(newscast.air_time);
  document.getElementById("news-meta").textContent = parts.join(" \u00b7 ");

  document.title = `Civic Media \u2014 ${newscast.title}`;

  updateStatusBadge();
}

function updateStatusBadge() {
  const badge = document.getElementById("status-badge");
  badge.textContent = newscast.status;
  badge.className = `nav-badge badge-${newscast.status === "complete" ? "complete" :
    newscast.status === "error" ? "error" : "processing"}`;
}

function showVideo() {
  const container = document.getElementById("video-container");
  container.style.display = "";
  // News video is served from the tv_news directory
  // For now, just hide upload area if source_file exists
  document.getElementById("upload-area").style.display = "none";
}

function showProgress() {
  document.getElementById("progress-area").style.display = "";
}

function renderSegments() {
  const list = document.getElementById("segments-list");
  document.getElementById("segment-count").textContent =
    `${segments.length} segment${segments.length !== 1 ? "s" : ""}`;

  if (segments.length === 0) {
    list.innerHTML = '<div class="empty-state">No story segments detected</div>';
    return;
  }

  list.innerHTML = "";
  segments.forEach(seg => {
    const card = document.createElement("div");
    card.className = "news-segment-card";

    const timeStr = seg.start_time != null
      ? `${fmtTime(seg.start_time)} \u2013 ${fmtTime(seg.end_time)}`
      : "";

    card.innerHTML = `
      <div class="news-segment-header">
        <div class="news-segment-title">${esc(seg.title || "Untitled")}</div>
        ${seg.story_type ? `<span class="news-segment-type">${esc(seg.story_type)}</span>` : ""}
      </div>
      ${timeStr ? `<div class="news-segment-time">${timeStr}</div>` : ""}
      <div class="news-segment-body">
        ${seg.summary ? `<div class="news-segment-summary">${esc(seg.summary)}</div>` : ""}
        ${seg.transcript ? `<div class="news-segment-transcript">${esc(seg.transcript)}</div>` : ""}
        <div class="tag-pills" id="tags-${seg.segment_id}"></div>
        <div class="mention-pills" id="mentions-${seg.segment_id}"></div>
      </div>
    `;

    // Toggle expanded
    card.querySelector(".news-segment-header").addEventListener("click", () => {
      card.classList.toggle("expanded");
    });

    // Click time to seek video
    const timeEl = card.querySelector(".news-segment-time");
    if (timeEl && seg.start_time != null) {
      timeEl.style.cursor = "pointer";
      timeEl.addEventListener("click", e => {
        e.stopPropagation();
        const video = document.getElementById("news-video");
        if (video) {
          video.currentTime = seg.start_time;
          video.play();
        }
      });
    }

    list.appendChild(card);

    // Load tags and mentions for this segment
    loadContentTags("tv_news_segment", seg.segment_id);
    loadContentMentions("tv_news_segment", seg.segment_id);
  });
}

// ── Tags & Mentions ──────────────────────────────────────────────────────────

async function loadContentTags(contentType, contentId) {
  try {
    const r = await fetch(`/api/tags/for-content?content_type=${contentType}&content_id=${contentId}`);
    if (!r.ok) return;
    const assignments = await r.json();
    renderTagPills(contentId, assignments);
  } catch { /* ignore */ }
}

async function loadContentMentions(contentType, contentId) {
  try {
    const r = await fetch(`/api/mentions/for-content?content_type=${contentType}&content_id=${contentId}`);
    if (!r.ok) return;
    const mentions = await r.json();
    renderMentionPills(contentId, mentions);
  } catch { /* ignore */ }
}

function renderTagPills(contentId, assignments) {
  const container = document.getElementById(`tags-${contentId}`);
  if (!container || assignments.length === 0) return;

  container.innerHTML = "";
  assignments.forEach(a => {
    const pill = document.createElement("span");
    const sourceClass = a.source === "confirmed" ? "confirmed" : a.source === "llm" ? "llm" : "";
    pill.className = `tag-pill ${sourceClass}`;
    pill.innerHTML = `
      ${esc(a.tag?.name || "tag")}
      <button class="pill-action" onclick="confirmTag('${a.assignment_id}')" title="Confirm">&#x2713;</button>
      <button class="pill-action" onclick="denyTag('${a.tag_id}','${a.content_type}','${a.content_id}')" title="Deny">&#x2715;</button>
    `;
    container.appendChild(pill);
  });
}

function renderMentionPills(contentId, mentions) {
  const container = document.getElementById(`mentions-${contentId}`);
  if (!container || mentions.length === 0) return;

  container.innerHTML = "";
  mentions.forEach(m => {
    const pill = document.createElement("span");
    pill.className = "mention-pill";
    pill.innerHTML = `
      ${esc(m.person_id)}
      <button class="pill-action" onclick="confirmMention('${m.mention_id}')" title="Confirm">&#x2713;</button>
      <button class="pill-action" onclick="denyMention('${m.person_id}','${m.content_type}','${m.content_id}')" title="Deny">&#x2715;</button>
    `;
    container.appendChild(pill);
  });
}

// Global tag/mention actions
window.confirmTag = async function(assignmentId) {
  await fetch(`/api/tags/assignments/${assignmentId}/confirm`, { method: "POST" });
  loadSegments();
};

window.denyTag = async function(tagId, contentType, contentId) {
  await fetch("/api/tags/deny", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tag_id: tagId, content_type: contentType, content_id: contentId }),
  });
  loadSegments();
};

window.confirmMention = async function(mentionId) {
  await fetch(`/api/mentions/confirm/${mentionId}`, { method: "POST" });
  loadSegments();
};

window.denyMention = async function(personId, contentType, contentId) {
  await fetch("/api/mentions/deny", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_id: personId, content_type: contentType, content_id: contentId }),
  });
  loadSegments();
};

// ── Polling ──────────────────────────────────────────────────────────────────

function startPolling() {
  if (pollerId) return;

  pollerId = setInterval(async () => {
    try {
      const r = await fetch(`/api/news/${newscastId}/status`);
      if (!r.ok) return;
      const status = await r.json();

      newscast.status = status.status;
      updateStatusBadge();

      const progressBar = document.getElementById("progress-bar");
      if (progressBar && status.stage) {
        progressBar.innerHTML = `
          <div class="pipeline-progress">
            <div class="progress-header">
              <span class="progress-stage">${esc(status.stage)}</span>
              <span class="progress-pct">${status.status === "complete" ? "\u2713 done" : (status.progress_pct || 0) + "%"}</span>
            </div>
            <div class="progress-bar-track">
              <div class="progress-bar-fill" style="width: ${status.progress_pct || 0}%"></div>
            </div>
            ${status.detail ? `<div class="progress-detail">${esc(status.detail)}</div>` : ""}
          </div>
        `;
      }

      if (status.status === "complete") {
        clearInterval(pollerId);
        pollerId = null;
        await loadSegments();
      } else if (status.status === "error") {
        clearInterval(pollerId);
        pollerId = null;
        if (status.error_detail) {
          document.getElementById("progress-bar").innerHTML =
            `<div class="progress-detail" style="color:var(--accent-red)">${esc(status.error_detail)}</div>`;
        }
      }
    } catch { /* ignore poll errors */ }
  }, POLL_INTERVAL);
}

// ── Utilities ────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
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

function fmtTime(sec) {
  if (sec == null) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

// ── Boot ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
