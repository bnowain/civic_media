/**
 * speakers.js — Speakers page logic.
 * Split-view directory with search, sort, keyboard nav, and detail panel.
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

let people = [];
let sortedPeople = [];
let selectedPersonId = null;
let selectedPerson = null;
let focusIndex = -1;
let searchDebounce = null;

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  await loadPeople();

  const searchInput = document.getElementById("search-input");
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => loadPeople(searchInput.value), 300);
  });

  document.getElementById("sort-select").addEventListener("change", () => {
    applySortAndRender();
  });

  document.getElementById("date-from").addEventListener("change", () => {
    if (selectedPersonId) loadAppearances(selectedPersonId);
  });
  document.getElementById("date-to").addEventListener("change", () => {
    if (selectedPersonId) loadAppearances(selectedPersonId);
  });

  document.getElementById("detail-rename-btn").addEventListener("click", () => {
    if (selectedPerson) handleRename(selectedPerson);
  });
  document.getElementById("detail-delete-btn").addEventListener("click", () => {
    if (selectedPerson) handleDelete(selectedPerson);
  });

  // Keyboard navigation
  document.addEventListener("keydown", handleKeyNav);
}

// ── API ───────────────────────────────────────────────────────────────────────

async function loadPeople(q = "") {
  try {
    const url = q ? `/api/people/?q=${encodeURIComponent(q)}` : "/api/people/";
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    people = await r.json();
    applySortAndRender();
  } catch (err) {
    console.error("Failed to load people:", err);
    document.getElementById("people-list").innerHTML =
      `<div class="spk-empty-state">Failed to load speakers.</div>`;
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
    // Update meetings pill
    document.getElementById("detail-mtg-pill").textContent =
      `${appearances.length} meeting${appearances.length !== 1 ? "s" : ""}`;
  } catch (err) {
    console.error("Failed to load appearances:", err);
    document.getElementById("appearances-list").innerHTML =
      `<div class="spk-empty-state">Failed to load appearances.</div>`;
  }
}

// ── Sort ──────────────────────────────────────────────────────────────────────

function applySortAndRender() {
  const sortBy = document.getElementById("sort-select").value;
  sortedPeople = [...people];

  if (sortBy === "voiceprints") {
    sortedPeople.sort((a, b) => b.voiceprint_count - a.voiceprint_count);
  } else if (sortBy === "az") {
    sortedPeople.sort((a, b) => a.canonical_name.localeCompare(b.canonical_name));
  } else if (sortBy === "za") {
    sortedPeople.sort((a, b) => b.canonical_name.localeCompare(a.canonical_name));
  }

  renderPeople();
}

// ── Render: People List ───────────────────────────────────────────────────────

function renderPeople() {
  const list = document.getElementById("people-list");
  const subtitle = document.getElementById("speakers-subtitle");

  subtitle.textContent = people.length === 0
    ? "No speakers found."
    : `${people.length} speaker${people.length !== 1 ? "s" : ""} \u00b7 Select a speaker to see their meeting appearances.`;

  if (sortedPeople.length === 0) {
    list.innerHTML = `
      <div class="spk-empty-state">
        No speakers found. Confirm speaker assignments in the review page to build voiceprints.
      </div>`;
    focusIndex = -1;
    return;
  }

  list.innerHTML = "";
  sortedPeople.forEach((p, i) => {
    const card = createPersonCard(p, i);
    list.appendChild(card);
  });

  // Restore focus index
  if (selectedPersonId) {
    focusIndex = sortedPeople.findIndex(p => p.person_id === selectedPersonId);
  }
}

function getInitials(name) {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

function createPersonCard(p, index) {
  const card = document.createElement("div");
  const isSelected = p.person_id === selectedPersonId;
  card.className = "spk-card" + (isSelected ? " selected" : "");
  card.dataset.personId = p.person_id;
  card.dataset.index = index;
  card.tabIndex = -1;

  const initials = getInitials(p.canonical_name);

  card.innerHTML = `
    <div class="spk-card-avatar">${initials}</div>
    <div class="spk-card-body">
      <div class="spk-card-name">${esc(p.canonical_name)}</div>
      <div class="spk-card-meta">${p.voiceprint_count} voiceprint${p.voiceprint_count !== 1 ? "s" : ""}</div>
    </div>
    <div class="spk-card-right">
      <span class="pill pill-green pill-sm">${p.voiceprint_count}</span>
      <span class="spk-card-chevron">&#8250;</span>
    </div>
  `;

  card.addEventListener("click", () => {
    focusIndex = index;
    selectPerson(p);
  });

  return card;
}

// ── Render: Detail Panel ──────────────────────────────────────────────────────

function selectPerson(p) {
  selectedPersonId = p.person_id;
  selectedPerson = p;

  // Update card highlights
  document.querySelectorAll(".spk-card").forEach(c => {
    c.classList.toggle("selected", c.dataset.personId === p.person_id);
  });

  // Populate detail header
  document.getElementById("detail-empty").style.display = "none";
  document.getElementById("detail-content").style.display = "";
  document.getElementById("detail-avatar").textContent = getInitials(p.canonical_name);
  document.getElementById("detail-name").textContent = p.canonical_name;
  document.getElementById("detail-vp-pill").textContent =
    `${p.voiceprint_count} voiceprint${p.voiceprint_count !== 1 ? "s" : ""}`;
  document.getElementById("detail-mtg-pill").textContent = "loading...";

  loadAppearances(p.person_id);
  loadMentions(p.person_id);
}

// ── Tab switching ────────────────────────────────────────────────────────────

document.addEventListener("click", e => {
  const tab = e.target.closest(".spk-tab");
  if (!tab) return;
  const tabName = tab.dataset.tab;

  document.querySelectorAll(".spk-tab").forEach(t => t.classList.remove("active"));
  tab.classList.add("active");

  document.getElementById("tab-appearances").style.display = tabName === "appearances" ? "" : "none";
  document.getElementById("tab-mentions").style.display = tabName === "mentions" ? "" : "none";
});

// ── Mentions ─────────────────────────────────────────────────────────────────

async function loadMentions(personId) {
  const list = document.getElementById("mentions-list");
  try {
    const r = await fetch(`/api/mentions/person/${personId}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const mentions = await r.json();
    renderMentions(mentions);
  } catch (err) {
    console.error("Failed to load mentions:", err);
    list.innerHTML = '<div class="spk-empty-state">Failed to load mentions.</div>';
  }
}

function renderMentions(mentions) {
  const list = document.getElementById("mentions-list");

  if (mentions.length === 0) {
    list.innerHTML = '<div class="spk-empty-state">No content mentions found for this person.</div>';
    return;
  }

  list.innerHTML = "";
  mentions.forEach(m => {
    const card = document.createElement("div");
    card.className = "spk-appearance-card";

    const typeLabel = m.content_type === "tv_news_segment" ? "News Segment"
      : m.content_type === "meeting" ? "Meeting"
      : m.content_type;

    card.innerHTML = `
      <div class="spk-appearance-body">
        <div class="spk-appearance-title">${esc(typeLabel)}: ${esc(m.content_id.slice(0, 8))}...</div>
        <div class="spk-appearance-meta">
          ${m.source ? `Source: ${esc(m.source)}` : ""}
          ${m.confidence != null ? ` \u00b7 Confidence: ${(m.confidence * 100).toFixed(0)}%` : ""}
        </div>
        ${m.context ? `<div class="spk-appearance-meta" style="margin-top:4px">${esc(m.context)}</div>` : ""}
      </div>
      <div class="spk-appearance-right">
        <span class="mention-pill">${esc(m.source || "mention")}</span>
      </div>
    `;

    list.appendChild(card);
  });
}

function renderAppearances(appearances) {
  const list = document.getElementById("appearances-list");

  if (appearances.length === 0) {
    list.innerHTML = `<div class="spk-empty-state">No meeting appearances found.</div>`;
    return;
  }

  list.innerHTML = "";
  appearances.forEach(a => {
    const card = document.createElement("div");
    card.className = "spk-appearance-card";

    const displayDate = formatDate(a.meeting_date);
    const segLabel = a.segment_count === 1 ? "segment" : "segments";
    const isAudio = a.category === "audio";

    const metaParts = [displayDate];
    if (!isAudio && a.group_name) metaParts.push(a.group_name);
    if (!isAudio && a.meeting_type) metaParts.push(a.meeting_type);
    if (isAudio) metaParts.push("Audio");
    const metaLine = metaParts.join(" \u00b7 ");

    card.innerHTML = `
      <div class="spk-appearance-body">
        <div class="spk-appearance-title">${esc(a.title || "Untitled")}</div>
        <div class="spk-appearance-meta">${esc(metaLine)}</div>
      </div>
      <div class="spk-appearance-right">
        <span class="pill pill-blue pill-sm">${a.segment_count} ${segLabel}</span>
      </div>
      <div class="spk-appearance-actions">
        <a href="/review/${a.meeting_id}?speaker=${selectedPersonId}" class="spk-appearance-link">Open meeting</a>
      </div>
    `;

    card.addEventListener("click", (e) => {
      if (e.target.closest(".spk-appearance-link")) return; // let the link handle it
      window.location.href = `/review/${a.meeting_id}?speaker=${selectedPersonId}`;
    });

    list.appendChild(card);
  });
}

// ── Keyboard Navigation ───────────────────────────────────────────────────────

function handleKeyNav(e) {
  // Only handle when not focused on an input
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA") return;

  if (e.key === "ArrowDown" || e.key === "j") {
    e.preventDefault();
    if (focusIndex < sortedPeople.length - 1) {
      focusIndex++;
      selectPerson(sortedPeople[focusIndex]);
      scrollCardIntoView(focusIndex);
    }
  } else if (e.key === "ArrowUp" || e.key === "k") {
    e.preventDefault();
    if (focusIndex > 0) {
      focusIndex--;
      selectPerson(sortedPeople[focusIndex]);
      scrollCardIntoView(focusIndex);
    }
  } else if (e.key === "Enter" && selectedPerson) {
    // Could open first meeting, but for now just focus search
  } else if (e.key === "/" || e.key === "s") {
    if (!e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      document.getElementById("search-input").focus();
    }
  }
}

function scrollCardIntoView(index) {
  const card = document.querySelector(`.spk-card[data-index="${index}"]`);
  if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
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

    // Refresh list and re-select
    const searchVal = document.getElementById("search-input").value;
    await loadPeople(searchVal);

    // Update detail panel name
    if (selectedPersonId === p.person_id) {
      const updated = sortedPeople.find(sp => sp.person_id === p.person_id);
      if (updated) selectPerson(updated);
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
      selectedPerson = null;
      focusIndex = -1;
      document.getElementById("detail-empty").style.display = "";
      document.getElementById("detail-content").style.display = "none";
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
