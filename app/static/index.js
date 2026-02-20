/**
 * index.js — Meeting list page logic.
 * Creates meetings, displays them as cards, navigates to review page.
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

let meetings = [];
let speakers = [];
const activePollers = {}; // meetingId -> intervalId
const POLL_INTERVAL = 4000;

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const dateInput = document.getElementById("f-meeting-date");
  dateInput.value = new Date().toISOString().slice(0, 10);

  await Promise.all([loadMeetings(), loadSpeakers()]);

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
}

// ── API ───────────────────────────────────────────────────────────────────────

async function loadMeetings() {
  try {
    const r = await fetch("/api/meetings/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    meetings = await r.json();
    renderMeetings();
  } catch (err) {
    console.error("Failed to load meetings:", err);
    document.getElementById("meetings-list").innerHTML =
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

function renderMeetings() {
  const list = document.getElementById("meetings-list");
  const count = document.getElementById("meeting-count");

  count.textContent = `${meetings.length} meeting${meetings.length !== 1 ? "s" : ""}`;

  if (meetings.length === 0) {
    list.innerHTML = `
      <div class="loading-state">
        No meetings yet. Create your first meeting to get started.
      </div>`;
    return;
  }

  // Stop any existing pollers before re-rendering
  Object.keys(activePollers).forEach(id => {
    clearInterval(activePollers[id]);
    delete activePollers[id];
  });

  list.innerHTML = "";
  meetings.forEach(m => {
    const card = createMeetingCard(m);
    list.appendChild(card);
  });

  // Kick off status checks for all cards
  meetings.forEach(m => updateCardStatus(m.meeting_id));
}

function createMeetingCard(m) {
  const card = document.createElement("div");
  card.className = "meeting-card";
  card.dataset.meetingId = m.meeting_id;

  const date = m.meeting_date || "—";
  const displayDate = formatDate(date);

  card.innerHTML = `
    <span class="meeting-card-date">${esc(displayDate)}</span>
    <div class="meeting-card-body">
      <div class="meeting-card-title">${esc(m.title)}</div>
      <div class="meeting-card-sub">${esc(m.governing_body)} · ${esc(m.meeting_type)}</div>
      <div class="meeting-progress" id="progress-${m.meeting_id}"></div>
    </div>
    <span class="meeting-card-badge badge-pending" id="badge-${m.meeting_id}">—</span>
    <button class="btn btn-ghost btn-sm delete-btn" data-meeting-id="${m.meeting_id}" title="Delete meeting">✕</button>
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
        <span class="progress-pct">${isComplete ? "✓ done" : pct ? pct + "%" : ""}</span>
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
    badge.textContent = "no video";
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

// ── Speakers ──────────────────────────────────────────────────────────────

async function loadSpeakers() {
  try {
    const r = await fetch("/api/people/");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    speakers = await r.json();
    renderSpeakers();
  } catch (err) {
    console.error("Failed to load speakers:", err);
    document.getElementById("speakers-list").innerHTML =
      `<div class="loading-state">Failed to load speakers.</div>`;
  }
}

function renderSpeakers() {
  const list = document.getElementById("speakers-list");
  const count = document.getElementById("speaker-count");

  count.textContent = `${speakers.length} speaker${speakers.length !== 1 ? "s" : ""}`;

  if (speakers.length === 0) {
    list.innerHTML = `
      <div class="loading-state">
        No speakers yet. Confirm speaker assignments in the review page to build voiceprints.
      </div>`;
    return;
  }

  list.innerHTML = "";
  speakers.forEach(s => {
    const card = createSpeakerCard(s);
    list.appendChild(card);
  });
}

function createSpeakerCard(s) {
  const card = document.createElement("div");
  card.className = "meeting-card speaker-card";
  card.dataset.personId = s.person_id;

  const vpLabel = s.voiceprint_count === 1 ? "voiceprint" : "voiceprints";

  card.innerHTML = `
    <span class="speaker-card-icon">&#9673;</span>
    <div class="meeting-card-body">
      <div class="meeting-card-title speaker-name" data-person-id="${s.person_id}">${esc(s.canonical_name)}</div>
      <div class="meeting-card-sub">${s.voiceprint_count} ${vpLabel}</div>
    </div>
    <button class="btn btn-ghost btn-sm rename-btn" data-person-id="${s.person_id}" title="Rename speaker">&#9998;</button>
    <button class="btn btn-ghost btn-sm delete-btn" data-person-id="${s.person_id}" title="Delete speaker">&#10005;</button>
  `;

  card.querySelector(".rename-btn").addEventListener("click", e => {
    e.stopPropagation();
    handleRenameSpeaker(s);
  });

  card.querySelector(".delete-btn").addEventListener("click", e => {
    e.stopPropagation();
    handleDeleteSpeaker(s);
  });

  return card;
}

async function handleRenameSpeaker(s) {
  const newName = prompt(`Rename "${s.canonical_name}" to:`, s.canonical_name);
  if (!newName || newName.trim() === s.canonical_name) return;

  try {
    const r = await fetch(`/api/people/${s.person_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canonical_name: newName.trim() }),
    });
    if (r.status === 409) {
      alert("A speaker with that name already exists.");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadSpeakers();
  } catch (err) {
    alert(`Failed to rename: ${err.message}`);
  }
}

async function handleDeleteSpeaker(s) {
  if (!confirm(
    `Delete "${s.canonical_name}" and all ${s.voiceprint_count} voiceprints?\n\nThis cannot be undone.`
  )) return;

  try {
    const r = await fetch(`/api/people/${s.person_id}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadSpeakers();
  } catch (err) {
    alert(`Failed to delete: ${err.message}`);
  }
}

// ── Dialog ────────────────────────────────────────────────────────────────────

function openDialog() {
  document.getElementById("new-meeting-dialog").showModal();
  document.getElementById("overlay").classList.add("active");
  document.getElementById("f-governing-body").focus();
}

function closeDialog() {
  document.getElementById("new-meeting-dialog").close();
  document.getElementById("overlay").classList.remove("active");
}

async function handleCreateMeeting() {
  const body  = document.getElementById("f-governing-body").value.trim();
  const type  = document.getElementById("f-meeting-type").value;
  const date  = document.getElementById("f-meeting-date").value;
  const title = document.getElementById("f-title").value.trim();

  if (!body || !date || !title) {
    alert("Please fill in Governing Body, Date, and Title.");
    return;
  }

  const btn = document.getElementById("create-meeting-btn");
  btn.textContent = "Creating...";
  btn.disabled = true;

  try {
    const meeting = await createMeeting({
      governing_body: body,
      meeting_type:   type,
      meeting_date:   date,
      title:          title,
    });
    closeDialog();
    window.location.href = `/review/${meeting.meeting_id}`;
  } catch (err) {
    alert(`Failed to create meeting: ${err.message}`);
  } finally {
    btn.textContent = "Create Meeting";
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
  if (!iso || iso === "—") return "—";
  try {
    const [y, m, d] = iso.split("-");
    return new Date(+y, +m - 1, +d).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return iso; }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
