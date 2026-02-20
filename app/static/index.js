/**
 * index.js — Meeting list page logic.
 * Creates meetings & audio transcriptions, displays them as cards,
 * navigates to review page.
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

let allItems = [];
let selectedCategory = "meeting";   // "meeting" | "audio"
const activePollers = {}; // meetingId -> intervalId
const POLL_INTERVAL = 4000;

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById("f-meeting-date").value = today;
  document.getElementById("f-audio-date").value = today;

  await loadMeetings();

  document.getElementById("new-meeting-btn")
    .addEventListener("click", openDialog);
  document.getElementById("close-dialog-btn")
    .addEventListener("click", closeDialog);
  document.getElementById("cancel-dialog-btn")
    .addEventListener("click", closeDialog);
  document.getElementById("create-meeting-btn")
    .addEventListener("click", handleCreateMeeting);
  document.getElementById("overlay")
    .addEventListener("click", closeDialog);

  document.getElementById("f-title")
    .addEventListener("keydown", e => { if (e.key === "Enter") handleCreateMeeting(); });

  // Category toggle
  document.getElementById("category-toggle").addEventListener("click", e => {
    const btn = e.target.closest(".cat-btn");
    if (!btn) return;
    selectedCategory = btn.dataset.cat;
    document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");

    const isMeeting = selectedCategory === "meeting";
    document.getElementById("meeting-fields").style.display = isMeeting ? "" : "none";
    document.getElementById("audio-fields").style.display = isMeeting ? "none" : "";
    document.getElementById("dialog-title").textContent =
      isMeeting ? "New Meeting" : "New Audio Transcription";
    document.getElementById("f-title").placeholder =
      isMeeting ? "e.g. January Regular Meeting" : "e.g. KQED Forum - Feb 19";
  });
}

// ── API ───────────────────────────────────────────────────────────────────────

async function loadMeetings() {
  try {
    const r = await fetch("/api/meetings/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    allItems = await r.json();
    renderAll();
  } catch (err) {
    console.error("Failed to load meetings:", err);
    document.getElementById("meetings-grid").innerHTML =
      `<div class="loading-state">Failed to load meetings. Is the server running?</div>`;
  }
}

async function createMeeting(payload) {
  const r = await fetch("/api/meetings/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (r.status === 422) {
    const err = await r.json();
    throw new Error(err.detail?.[0]?.msg || "Validation error");
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function getMeetingStatus(meetingId) {
  try {
    const r = await fetch(`/api/media/${meetingId}/status`);
    if (!r.ok) return null;
    return r.json();
  } catch { return null; }
}

// ── Render ────────────────────────────────────────────────────────────────────

function renderAll() {
  const meetingItems = allItems.filter(m => (m.category || "meeting") === "meeting");
  const audioItems = allItems.filter(m => m.category === "audio");

  // Stop any existing pollers before re-rendering
  Object.keys(activePollers).forEach(id => {
    clearInterval(activePollers[id]);
    delete activePollers[id];
  });

  renderSection("meetings-grid", "meeting-count", meetingItems, "meeting");
  renderSection("audio-grid", "audio-count", audioItems, "audio");

  // Kick off status checks for all cards
  allItems.forEach(m => updateCardStatus(m.meeting_id));
}

function renderSection(gridId, countId, items, type) {
  const grid = document.getElementById(gridId);
  const countEl = document.getElementById(countId);

  const label = type === "meeting" ? "meeting" : "item";
  countEl.textContent = `${items.length} ${label}${items.length !== 1 ? "s" : ""}`;

  if (items.length === 0) {
    const emptyMsg = type === "meeting"
      ? "No meetings yet. Create your first meeting to get started."
      : "No audio transcriptions yet.";
    grid.innerHTML = `<div class="loading-state">${emptyMsg}</div>`;
    return;
  }

  grid.innerHTML = "";
  items.forEach(m => {
    const card = createMeetingCard(m);
    grid.appendChild(card);
  });
}

function createMeetingCard(m) {
  const card = document.createElement("div");
  card.className = "meeting-card";
  card.dataset.meetingId = m.meeting_id;

  const date = m.meeting_date || "\u2014";
  const displayDate = formatDate(date);
  const isAudio = m.category === "audio";

  const subLine = isAudio
    ? (m.governing_body ? esc(m.governing_body) : "Audio Transcription")
    : `${esc(m.governing_body)} \u00b7 ${esc(m.meeting_type)}`;

  card.innerHTML = `
    <span class="meeting-card-date">${esc(displayDate)}</span>
    <div class="meeting-card-body">
      <div class="meeting-card-title">${esc(m.title)}</div>
      <div class="meeting-card-sub">${subLine}</div>
      <div class="meeting-progress" id="progress-${m.meeting_id}"></div>
    </div>
    <span class="meeting-card-badge badge-pending" id="badge-${m.meeting_id}">\u2014</span>
    <button class="btn btn-ghost btn-sm delete-btn" data-meeting-id="${m.meeting_id}" title="Delete">\u2715</button>
  `;

  card.addEventListener("click", e => {
    if (e.target.closest(".delete-btn")) return;
    window.location.href = `/review/${m.meeting_id}`;
  });

  card.querySelector(".delete-btn").addEventListener("click", async e => {
    e.stopPropagation();
    if (!confirm(`Delete "${m.title}"? This cannot be undone.`)) return;
    await deleteMeeting(m.meeting_id);
  });

  return card;
}

async function deleteMeeting(meetingId) {
  const r = await fetch(`/api/meetings/${meetingId}`, { method: "DELETE" });
  if (r.ok) {
    await loadMeetings();
  } else {
    alert("Failed to delete meeting.");
  }
}

// ── Progress & Status ─────────────────────────────────────────────────────────

function renderProgressBar(progressEl, status) {
  const pct = status.progress_pct ?? 0;
  const stage = status.stage || "";
  const detail = status.detail || "";
  const isComplete = status.status === "complete";
  const indeterminate = !isComplete && pct === 0 && stage !== "";

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

function applyStatusToBadge(badge, status) {
  if (!badge) return;
  if (status.status === "complete" && status.segment_count > 0) {
    badge.textContent = `${status.segment_count} segs`;
    badge.className = "meeting-card-badge badge-complete";
  } else if (status.status === "processing") {
    badge.textContent = "processing";
    badge.className = "meeting-card-badge badge-processing";
  } else {
    badge.textContent = "no media";
    badge.className = "meeting-card-badge badge-pending";
  }
}

async function updateCardStatus(meetingId) {
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

  applyStatusToBadge(badge, status);

  if (status.status === "processing" && progressEl) {
    renderProgressBar(progressEl, status);
    startPolling(meetingId);
  } else if (status.status === "complete" && progressEl) {
    renderProgressBar(progressEl, status);
  }
}

function startPolling(meetingId) {
  if (activePollers[meetingId]) return; // already polling

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
      // Meeting was deleted — stop polling
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
      return;
    }

    applyStatusToBadge(badge, status);
    if (progressEl) renderProgressBar(progressEl, status);

    if (status.status === "complete") {
      clearInterval(activePollers[meetingId]);
      delete activePollers[meetingId];
    }
  }, POLL_INTERVAL);
}

// ── Dialog ────────────────────────────────────────────────────────────────────

function openDialog() {
  // Reset to meeting mode
  selectedCategory = "meeting";
  document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
  document.querySelector('.cat-btn[data-cat="meeting"]').classList.add("active");
  document.getElementById("meeting-fields").style.display = "";
  document.getElementById("audio-fields").style.display = "none";
  document.getElementById("dialog-title").textContent = "New Meeting";
  document.getElementById("f-title").placeholder = "e.g. January Regular Meeting";

  document.getElementById("new-meeting-dialog").showModal();
  document.getElementById("overlay").classList.add("active");
  document.getElementById("f-governing-body").focus();
}

function closeDialog() {
  document.getElementById("new-meeting-dialog").close();
  document.getElementById("overlay").classList.remove("active");
}

async function handleCreateMeeting() {
  const isMeeting = selectedCategory === "meeting";
  const title = document.getElementById("f-title").value.trim();
  const date = isMeeting
    ? document.getElementById("f-meeting-date").value
    : document.getElementById("f-audio-date").value;

  if (isMeeting) {
    const body = document.getElementById("f-governing-body").value.trim();
    if (!body || !date || !title) {
      alert("Please fill in Governing Body, Date, and Title.");
      return;
    }
  } else {
    if (!date || !title) {
      alert("Please fill in Date and Title.");
      return;
    }
  }

  const btn = document.getElementById("create-meeting-btn");
  btn.textContent = "Creating...";
  btn.disabled = true;

  try {
    const payload = {
      title,
      meeting_date: date,
      category: selectedCategory,
      governing_body: isMeeting ? document.getElementById("f-governing-body").value.trim() : "",
      meeting_type: isMeeting ? document.getElementById("f-meeting-type").value : "",
    };

    const meeting = await createMeeting(payload);
    closeDialog();
    window.location.href = `/review/${meeting.meeting_id}`;
  } catch (err) {
    alert(`Failed to create: ${err.message}`);
  } finally {
    btn.textContent = "Create";
    btn.disabled = false;
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

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

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
