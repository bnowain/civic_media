/**
 * speakers.js — Speakers page logic.
 * Lists all people, search/filter, shows meeting appearances per person.
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

let people = [];
let selectedPersonId = null;
let searchDebounce = null;

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  await loadPeople();

  const searchInput = document.getElementById("search-input");
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => loadPeople(searchInput.value), 300);
  });

  document.getElementById("date-from").addEventListener("change", () => {
    if (selectedPersonId) loadAppearances(selectedPersonId);
  });
  document.getElementById("date-to").addEventListener("change", () => {
    if (selectedPersonId) loadAppearances(selectedPersonId);
  });
}

// ── API ───────────────────────────────────────────────────────────────────────

async function loadPeople(q = "") {
  try {
    const url = q ? `/api/people/?q=${encodeURIComponent(q)}` : "/api/people/";
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    people = await r.json();
    renderPeople();
  } catch (err) {
    console.error("Failed to load people:", err);
    document.getElementById("people-list").innerHTML =
      `<div class="loading-state">Failed to load speakers.</div>`;
  }
}

async function loadAppearances(personId) {
  const dateFrom = document.getElementById("date-from").value;
  const dateTo = document.getElementById("date-to").value;

  let url = `/api/people/${personId}/appearances`;
  const params = [];
  if (dateFrom) params.push(`date_from=${dateFrom}`);
  if (dateTo) params.push(`date_to=${dateTo}`);
  if (params.length) url += `?${params.join("&")}`;

  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const appearances = await r.json();
    renderAppearances(appearances);
  } catch (err) {
    console.error("Failed to load appearances:", err);
    document.getElementById("appearances-list").innerHTML =
      `<div class="loading-state">Failed to load appearances.</div>`;
  }
}

// ── Render ────────────────────────────────────────────────────────────────────

function renderPeople() {
  const list = document.getElementById("people-list");
  const count = document.getElementById("speaker-count");

  count.textContent = `${people.length} speaker${people.length !== 1 ? "s" : ""}`;

  if (people.length === 0) {
    list.innerHTML = `
      <div class="loading-state">
        No speakers found. Confirm speaker assignments in the review page to build voiceprints.
      </div>`;
    return;
  }

  list.innerHTML = "";
  people.forEach(p => {
    const card = createPersonCard(p);
    list.appendChild(card);
  });
}

function createPersonCard(p) {
  const card = document.createElement("div");
  card.className = "person-card" + (p.person_id === selectedPersonId ? " selected" : "");
  card.dataset.personId = p.person_id;

  const vpLabel = p.voiceprint_count === 1 ? "voiceprint" : "voiceprints";

  card.innerHTML = `
    <div class="person-card-body">
      <div class="person-card-name">${esc(p.canonical_name)}</div>
      <div class="person-card-sub">${p.voiceprint_count} ${vpLabel}</div>
    </div>
    <div class="person-card-actions">
      <button class="btn btn-ghost btn-sm rename-btn" title="Rename">&#9998;</button>
      <button class="btn btn-ghost btn-sm delete-btn" title="Delete">&#10005;</button>
    </div>
  `;

  card.addEventListener("click", e => {
    if (e.target.closest(".rename-btn") || e.target.closest(".delete-btn")) return;
    selectPerson(p);
  });

  card.querySelector(".rename-btn").addEventListener("click", e => {
    e.stopPropagation();
    handleRename(p);
  });

  card.querySelector(".delete-btn").addEventListener("click", e => {
    e.stopPropagation();
    handleDelete(p);
  });

  return card;
}

function selectPerson(p) {
  selectedPersonId = p.person_id;

  // Update selection highlight
  document.querySelectorAll(".person-card").forEach(c => {
    c.classList.toggle("selected", c.dataset.personId === p.person_id);
  });

  // Show appearances panel
  document.getElementById("appearances-empty").style.display = "none";
  document.getElementById("appearances-content").style.display = "";
  document.getElementById("appearances-name").textContent = p.canonical_name;

  loadAppearances(p.person_id);
}

function renderAppearances(appearances) {
  const list = document.getElementById("appearances-list");

  if (appearances.length === 0) {
    list.innerHTML = `<div class="loading-state">No meeting appearances found.</div>`;
    return;
  }

  list.innerHTML = "";
  appearances.forEach(a => {
    const item = document.createElement("a");
    item.className = "appearance-item";
    item.href = `/review/${a.meeting_id}`;

    const displayDate = formatDate(a.meeting_date);
    const segLabel = a.segment_count === 1 ? "segment" : "segments";
    const isAudio = a.category === "audio";

    const subLine = isAudio
      ? "Audio Transcription"
      : [a.governing_body, a.meeting_type].filter(Boolean).join(" \u00b7 ");

    item.innerHTML = `
      <div class="appearance-info">
        <div class="appearance-title">${esc(a.title || "Untitled")}</div>
        <div class="appearance-sub">${esc(subLine)}</div>
      </div>
      <div class="appearance-meta">
        <span class="appearance-date">${esc(displayDate)}</span>
        <span class="appearance-segs">${a.segment_count} ${segLabel}</span>
      </div>
    `;

    list.appendChild(item);
  });
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function handleRename(p) {
  const newName = prompt(`Rename "${p.canonical_name}" to:`, p.canonical_name);
  if (!newName || newName.trim() === p.canonical_name) return;

  try {
    const r = await fetch(`/api/people/${p.person_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canonical_name: newName.trim() }),
    });
    if (r.status === 409) {
      alert("A speaker with that name already exists.");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const searchVal = document.getElementById("search-input").value;
    await loadPeople(searchVal);
    if (selectedPersonId === p.person_id) {
      document.getElementById("appearances-name").textContent = newName.trim();
    }
  } catch (err) {
    alert(`Failed to rename: ${err.message}`);
  }
}

async function handleDelete(p) {
  if (!confirm(
    `Delete "${p.canonical_name}" and all ${p.voiceprint_count} voiceprints?\n\nThis cannot be undone.`
  )) return;

  try {
    const r = await fetch(`/api/people/${p.person_id}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);

    if (selectedPersonId === p.person_id) {
      selectedPersonId = null;
      document.getElementById("appearances-empty").style.display = "";
      document.getElementById("appearances-content").style.display = "none";
    }

    const searchVal = document.getElementById("search-input").value;
    await loadPeople(searchVal);
  } catch (err) {
    alert(`Failed to delete: ${err.message}`);
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
  if (!iso) return "\u2014";
  try {
    const [y, m, d] = iso.split("-");
    return new Date(+y, +m - 1, +d).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return iso; }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
