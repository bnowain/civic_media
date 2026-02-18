/**
 * index.js — Meeting list page logic.
 * Creates meetings, displays them as cards, navigates to review page.
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

let meetings = [];

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  // Set today as default date
  const dateInput = document.getElementById("f-meeting-date");
  dateInput.value = new Date().toISOString().slice(0, 10);

  await loadMeetings();

  // Wire up buttons
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

  // Allow Enter key to submit
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

  list.innerHTML = "";
  meetings.forEach(m => {
    const card = createMeetingCard(m);
    list.appendChild(card);
  });

  // Async badge updates — fire & forget
  meetings.forEach(m => updateCardBadge(m.meeting_id));
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
    </div>
    <span class="meeting-card-badge badge-pending" id="badge-${m.meeting_id}">—</span>
  `;

  card.addEventListener("click", () => {
    window.location.href = `/review/${m.meeting_id}`;
  });

  return card;
}

async function updateCardBadge(meetingId) {
  const badge = document.getElementById(`badge-${meetingId}`);
  if (!badge) return;

  const status = await getMeetingStatus(meetingId);
  if (!status) {
    badge.textContent = "pending";
    badge.className = "meeting-card-badge badge-pending";
    return;
  }

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
  const body     = document.getElementById("f-governing-body").value.trim();
  const type     = document.getElementById("f-meeting-type").value;
  const date     = document.getElementById("f-meeting-date").value;
  const title    = document.getElementById("f-title").value.trim();

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
    // Navigate directly to the review page for the new meeting
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
