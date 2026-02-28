/**
 * review.js — Side-by-side video review interface.
 *
 * Changes from previous version:
 *   - Confirmation overwrite bug fixed: confirmed cards are tracked in
 *     _confirmedThisSession and never overwritten by background polling.
 *   - Inline text editing: double-click a segment's text to edit it.
 *   - Export dropdown: SRT, TXT, JSON download.
 *   - Re-run pipeline button with confirmation dialog.
 *   - Progress bar in pipeline banner (polls every 4s).
 *   - Video source uses /media/{id}/video (range-request endpoint, no .mp4).
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

const meetingId = location.pathname.split("/review/")[1];

let meeting      = null;
let segments     = [];
let people       = [];
let documents    = [];
let activeIndex  = -1;
let pollTimer    = null;
let pendingSegmentId = null;   // for new-person dialog

// Segments confirmed in this browser session — never overwritten by polling
const _confirmedThisSession = new Set();

// Quick-assign state
const _personFrequency = new Map();  // person_id → count in this meeting
let _ignorePersonId = null;
const MIN_VOICEPRINT_DURATION = 4.0; // matches server-side voiceprint.MIN_VOICEPRINT_DURATION
const MAX_OVERLAP_FOR_VOICEPRINT = 0.15; // matches server-side config

// DOM shortcuts
const video          = () => document.getElementById("video-player");
const transcriptList = () => document.getElementById("transcript-list");

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function init() {
  if (!meetingId) {
    document.title = "Error — Civic Media";
    return;
  }

  await Promise.all([loadMeeting(), loadPeople()]);
  await _ensureIgnorePerson();
  await loadDocuments();
  await loadSegments();
  _buildPersonFrequency();

  setupVideoEvents();
  setupControls();
  setupDocumentUploads();
  setupPersonDialog();
  setupExportDropdown();
  setupRerunDialog();
  setupClipBuilder();
  setupSummaryPanel();
  renderSummaries();

  await checkPipelineAndPoll();
  seekToSpeakerFromURL();
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function loadMeeting() {
  try {
    const r = await fetch(`/api/meetings/${meetingId}`);
    if (r.status === 404) {
      // Meeting was deleted — redirect to home
      window.location.href = "/";
      return;
    }
    if (!r.ok) return;
    meeting = await r.json();
    document.getElementById("hdr-group").textContent = meeting.group_name;
    document.getElementById("hdr-title").textContent = meeting.title;
    document.title = `${meeting.title} — Civic Media`;

    // Source provenance link
    const srcLink = document.getElementById("hdr-source-link");
    if (meeting.page_url) {
      srcLink.href = meeting.page_url;
      srcLink.textContent = "\u{1F517} Source";
      srcLink.style.display = "";
    } else if (meeting.video_url) {
      srcLink.href = meeting.video_url;
      srcLink.textContent = "\u{1F517} Video Source";
      srcLink.style.display = "";
    } else if (meeting.source_url) {
      srcLink.href = meeting.source_url;
      srcLink.textContent = "\u{1F517} Audio File";
      srcLink.style.display = "";
    }
  } catch (err) {
    console.error("loadMeeting:", err);
  }
}

async function loadPeople() {
  try {
    const r = await fetch("/api/people/");
    if (!r.ok) return;
    people = await r.json();
  } catch (err) {
    console.error("loadPeople:", err);
  }
}

async function fetchSegments() {
  const filter = document.getElementById("filter-select").value;
  const url = `/api/segments/${meetingId}${filter ? `?filter=${filter}` : ""}`;
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    return r.json();
  } catch (err) {
    console.error("fetchSegments:", err);
    return null;
  }
}

async function loadSegments() {
  const fresh = await fetchSegments();
  if (!fresh) return;

  // Don't overwrite cards confirmed in this session
  segments = fresh.map(s => {
    if (_confirmedThisSession.has(s.segment_id)) {
      const existing = segments.find(x => x.segment_id === s.segment_id);
      return existing || s;
    }
    return s;
  });

  renderTranscript();
  updateStats();
  updateProcessButton();
}

async function loadDocuments() {
  try {
    const r = await fetch(`/api/documents/${meetingId}`);
    if (!r.ok) return;
    documents = await r.json();
    renderDocuments();
  } catch (err) {
    console.error("loadDocuments:", err);
  }
}

async function confirmAssignment(segmentId, personId) {
  const r = await fetch(`/api/assignments/${segmentId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_id: personId }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

async function tagAssignment(segmentId, personId) {
  const r = await fetch(`/api/assignments/${segmentId}/tag`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_id: personId }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

async function createPerson(name) {
  const r = await fetch("/api/people/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ canonical_name: name }),
  });
  if (r.status === 409) throw new Error("Person already exists");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function reprocessAll() {
  const r = await fetch(`/api/assignments/reprocess/${meetingId}`, { method: "POST" });
  return r.ok ? r.json() : null;
}

async function editSegmentText(segmentId, text) {
  const r = await fetch(`/api/segments/${segmentId}/edit`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// ── Interaction guard ─────────────────────────────────────────────────────────

function isUserInteracting() {
  const el = document.activeElement;
  if (!el) return false;
  if (el.classList.contains("seg-assign-input")) return true;
  if (el.getAttribute("contenteditable") === "true") return true;
  return false;
}

// ── Quick-Assign Helpers ──────────────────────────────────────────────────────

async function _ensureIgnorePerson() {
  const existing = people.find(p => p.canonical_name === "Ignore");
  if (existing) {
    _ignorePersonId = existing.person_id;
    return;
  }
  try {
    const person = await createPerson("Ignore");
    people.push(person);
    _ignorePersonId = person.person_id;
  } catch (err) {
    // May have been created concurrently — reload people list
    await loadPeople();
    const found = people.find(p => p.canonical_name === "Ignore");
    if (found) _ignorePersonId = found.person_id;
  }
}

function _buildPersonFrequency() {
  _personFrequency.clear();
  for (const seg of segments) {
    const pid = seg.assignment?.predicted_person_id;
    if (pid) _personFrequency.set(pid, (_personFrequency.get(pid) || 0) + 1);
  }
}

function _getQuickAssignPeople() {
  return Array.from(_personFrequency.entries())
    .filter(([pid]) => pid !== _ignorePersonId)
    .sort((a, b) => b[1] - a[1])
    .map(([pid]) => {
      const p = people.find(x => x.person_id === pid);
      return p ? { person_id: p.person_id, canonical_name: p.canonical_name } : null;
    })
    .filter(Boolean);
}

// ── Render Transcript ─────────────────────────────────────────────────────────

function renderTranscript() {
  const list = transcriptList();

  if (segments.length === 0) {
    list.innerHTML = `
      <div class="empty-state" id="transcript-empty">
        <span class="empty-icon">⬡</span>
        <p>No transcript yet.</p>
        <p class="empty-sub">Upload a video to begin processing.</p>
      </div>`;
    return;
  }

  const scrollTop = list.scrollTop;
  list.innerHTML = "";

  const groups = groupConsecutiveSpeakers(segments);

  groups.forEach(group => {
    if (group.speakerId && group.segments.length > 1) {
      // Grouped: shared header + contained segments
      const wrapper = document.createElement("div");
      wrapper.className = "segment-group";

      const header = document.createElement("div");
      header.className = "segment-group-header";
      header.textContent = group.speakerName;
      wrapper.appendChild(header);

      group.segments.forEach(seg => {
        const idx = segments.indexOf(seg);
        wrapper.appendChild(buildSegmentCard(seg, idx, true));
      });
      list.appendChild(wrapper);
    } else {
      // Ungrouped (no speaker or single segment): render individually
      group.segments.forEach(seg => {
        const idx = segments.indexOf(seg);
        list.appendChild(buildSegmentCard(seg, idx, false));
      });
    }
  });

  list.scrollTop = scrollTop;
  activeIndex = -1;
  syncActiveCard(video()?.currentTime ?? 0, false);
}

function groupConsecutiveSpeakers(segs) {
  const groups = [];
  for (const seg of segs) {
    const personId = seg.assignment?.predicted_person_id || null;
    const last = groups[groups.length - 1];
    if (last && last.speakerId === personId && personId !== null) {
      last.segments.push(seg);
    } else {
      const person = personId ? people.find(p => p.person_id === personId) : null;
      groups.push({
        speakerId: personId,
        speakerName: person?.canonical_name || seg.raw_speaker_label || "Unknown",
        segments: [seg],
      });
    }
  }
  return groups;
}

function buildSegmentCard(seg, idx, grouped = false) {
  const assign   = seg.assignment;
  const verified = assign?.verified ?? false;
  const tagged   = assign?.tagged ?? false;
  const score    = assign?.similarity_score ?? null;
  const personId = assign?.predicted_person_id ?? null;

  const confClass = verified ? "verified"
    : tagged                  ? "tagged"
    : score === null          ? "unknown"
    : score >= 0.92           ? "high"
    : score >= 0.75           ? "medium"
    : "unknown";

  const person = people.find(p => p.person_id === personId);
  const speakerName = person?.canonical_name ?? seg.raw_speaker_label ?? "Unknown";

  const scoreStr = score !== null ? `${Math.round(score * 100)}%` : "—";
  const badgeText = verified ? "✓ verified"
    : tagged ? "tagged"
    : confClass === "unknown" ? "unknown"
    : `${confClass} ${scoreStr}`;

  const card = document.createElement("div");
  card.className = `segment-card conf-${confClass}${grouped ? " grouped" : ""}`;
  card.dataset.segmentId = seg.segment_id;
  card.dataset.idx       = idx;
  card.dataset.start     = seg.start_time;
  card.dataset.end       = seg.end_time;

  const metaHtml = grouped
    ? `<div class="seg-meta">
        <span class="seg-time">${fmtTime(seg.start_time)}</span>
        <span class="seg-badge badge-${confClass}">${esc(badgeText)}</span>
      </div>`
    : `<div class="seg-meta">
        <span class="seg-time">${fmtTime(seg.start_time)}</span>
        <span class="seg-badge badge-${confClass}">${esc(badgeText)}</span>
        <span class="seg-speaker-name">${esc(speakerName)}</span>
      </div>`;

  card.innerHTML = `
    ${metaHtml}
    <div class="seg-text" title="Double-click to edit">${esc(seg.text)}</div>
    <div class="seg-edit-controls" id="edit-controls-${seg.segment_id}">
      <button class="btn btn-primary btn-xs seg-save-btn" data-seg="${seg.segment_id}">Save</button>
      <button class="btn btn-ghost btn-xs seg-cancel-btn" data-seg="${seg.segment_id}">Cancel</button>
    </div>
    <div class="seg-quick-row" data-seg="${seg.segment_id}" onclick="event.stopPropagation()"></div>
    <div class="seg-controls" onclick="event.stopPropagation()">
      <div class="seg-assign-wrap" data-seg="${seg.segment_id}">
        <input type="text" class="seg-assign-input" data-seg="${seg.segment_id}"
               data-person-id="${personId || ""}"
               value="${personId ? esc(speakerName) : ""}"
               placeholder="— Assign —" autocomplete="off" />
        <div class="seg-assign-dropdown"></div>
      </div>
      <button class="seg-tag-btn${tagged ? " is-tagged" : ""}"
              data-seg="${seg.segment_id}">
        ${tagged ? "✦ Tagged" : "Tag"}
      </button>
      <button class="seg-confirm-btn ${verified ? "confirmed" : ""}"
              data-seg="${seg.segment_id}">
        ${verified ? "✓" : "Confirm"}
      </button>
      <button class="seg-new-btn" data-seg="${seg.segment_id}">+ New</button>
    </div>
    <div class="tag-pills" id="seg-tags-${seg.segment_id}"></div>
  `;

  // Wire up autocomplete on the assign input
  _setupAssignAutocomplete(card);

  // Render quick-assign buttons
  _renderQuickButtons(seg.segment_id, card);

  // Load tags for this segment
  loadSegmentTags(seg.segment_id);

  // Seek on card body click
  card.addEventListener("click", e => {
    if (e.target.closest(".seg-controls")) return;
    if (e.target.closest(".seg-quick-row")) return;
    if (e.target.closest(".seg-edit-controls")) return;
    if (e.target.classList.contains("seg-text") && e.detail >= 2) return; // let dblclick handle
    const v = video();
    if (v) {
      v.currentTime = seg.start_time;
      v.play().catch(() => {});
    }
  });

  // Double-click text to edit
  const textEl = card.querySelector(".seg-text");
  textEl.addEventListener("dblclick", e => {
    e.stopPropagation();
    startTextEdit(card, seg, textEl);
  });

  // Save / cancel edit
  card.querySelector(".seg-save-btn").addEventListener("click", e => {
    e.stopPropagation();
    saveTextEdit(card, seg, textEl);
  });
  card.querySelector(".seg-cancel-btn").addEventListener("click", e => {
    e.stopPropagation();
    cancelTextEdit(card, seg, textEl);
  });

  // Tag button
  card.querySelector(".seg-tag-btn").addEventListener("click", async () => {
    const pid = _getAssignedPersonId(card);
    if (!pid) { alert("Select a speaker first."); return; }
    await handleTag(seg.segment_id, pid, card);
  });

  // Confirm button (auto-falls back to Tag for short/overlapping segments)
  card.querySelector(".seg-confirm-btn").addEventListener("click", async () => {
    const pid = _getAssignedPersonId(card);
    if (!pid) { alert("Select a speaker first."); return; }
    const duration = seg.end_time - seg.start_time;
    const overlap = seg.overlap_ratio || 0;
    if (duration < MIN_VOICEPRINT_DURATION || overlap > MAX_OVERLAP_FOR_VOICEPRINT) {
      await handleTag(seg.segment_id, pid, card);
    } else {
      await handleConfirm(seg.segment_id, pid, card);
    }
  });

  // New person button
  card.querySelector(".seg-new-btn").addEventListener("click", () => {
    pendingSegmentId = seg.segment_id;
    openPersonDialog();
  });

  return card;
}

// ── Autocomplete Assign Input ─────────────────────────────────────────────────

function _getAssignedPersonId(cardEl) {
  const input = cardEl.querySelector(".seg-assign-input");
  return input?.dataset.personId || "";
}

function _setAssignInput(cardEl, personId, name) {
  const input = cardEl.querySelector(".seg-assign-input");
  if (!input) return;
  input.dataset.personId = personId || "";
  input.value = name || "";
}

function _setupAssignAutocomplete(cardEl) {
  const input = cardEl.querySelector(".seg-assign-input");
  const dropdown = cardEl.querySelector(".seg-assign-dropdown");
  if (!input || !dropdown) return;

  let highlightIdx = -1;

  function getSortedPeople(query) {
    const q = query.toLowerCase();
    let filtered = q
      ? people.filter(p => p.canonical_name.toLowerCase().includes(q))
      : people.slice();

    // Sort: meeting-frequency desc, then alphabetical
    filtered.sort((a, b) => {
      const fa = _personFrequency.get(a.person_id) || 0;
      const fb = _personFrequency.get(b.person_id) || 0;
      if (fb !== fa) return fb - fa;
      return a.canonical_name.localeCompare(b.canonical_name);
    });

    return filtered;
  }

  function renderDropdown(query) {
    const results = getSortedPeople(query);
    dropdown.innerHTML = "";
    highlightIdx = -1;

    if (results.length === 0) {
      dropdown.innerHTML = '<div class="seg-assign-no-match">No matches</div>';
      dropdown.hidden = false;
      return;
    }

    const q = query.toLowerCase();
    results.forEach((p, i) => {
      const item = document.createElement("div");
      item.className = "seg-assign-option";
      item.dataset.personId = p.person_id;
      item.dataset.idx = i;

      // Highlight matching portion
      if (q) {
        const name = p.canonical_name;
        const matchIdx = name.toLowerCase().indexOf(q);
        if (matchIdx >= 0) {
          item.innerHTML =
            esc(name.slice(0, matchIdx)) +
            '<span class="seg-assign-match">' + esc(name.slice(matchIdx, matchIdx + q.length)) + '</span>' +
            esc(name.slice(matchIdx + q.length));
        } else {
          item.textContent = name;
        }
      } else {
        item.textContent = p.canonical_name;
      }

      // Frequency badge
      const freq = _personFrequency.get(p.person_id) || 0;
      if (freq > 0) {
        const badge = document.createElement("span");
        badge.className = "seg-assign-freq";
        badge.textContent = freq;
        item.appendChild(badge);
      }

      item.addEventListener("mousedown", (e) => {
        e.preventDefault(); // prevent blur
        selectPerson(p);
      });

      dropdown.appendChild(item);
    });

    dropdown.hidden = false;
  }

  function selectPerson(p) {
    input.dataset.personId = p.person_id;
    input.value = p.canonical_name;
    dropdown.hidden = true;
    highlightIdx = -1;
    input.blur();
  }

  function updateHighlight() {
    const items = dropdown.querySelectorAll(".seg-assign-option");
    items.forEach((el, i) => {
      el.classList.toggle("highlighted", i === highlightIdx);
    });
    // Scroll highlighted item into view
    if (highlightIdx >= 0 && items[highlightIdx]) {
      items[highlightIdx].scrollIntoView({ block: "nearest" });
    }
  }

  // On focus — show all candidates (unfiltered), select text so typing replaces it
  input.addEventListener("focus", () => {
    input.select();
    renderDropdown("");
  });

  // On input — filter
  input.addEventListener("input", () => {
    // Clear selection when user types
    input.dataset.personId = "";
    renderDropdown(input.value);
  });

  // Keyboard navigation
  input.addEventListener("keydown", (e) => {
    const items = dropdown.querySelectorAll(".seg-assign-option");
    const count = items.length;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      highlightIdx = Math.min(highlightIdx + 1, count - 1);
      updateHighlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      highlightIdx = Math.max(highlightIdx - 1, 0);
      updateHighlight();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlightIdx >= 0 && items[highlightIdx]) {
        const pid = items[highlightIdx].dataset.personId;
        const p = people.find(x => x.person_id === pid);
        if (p) selectPerson(p);
      }
    } else if (e.key === "Escape") {
      dropdown.hidden = true;
      input.blur();
    }
  });

  // On blur — close dropdown, restore value if no valid selection
  input.addEventListener("blur", () => {
    // Small delay to allow mousedown on dropdown items
    setTimeout(() => {
      dropdown.hidden = true;
      // If personId is cleared (user typed but didn't select), restore or clear
      if (!input.dataset.personId) {
        input.value = "";
      }
    }, 150);
  });
}

// ── Quick-Assign Buttons ──────────────────────────────────────────────────────

function _renderQuickButtons(segmentId, cardEl) {
  const row = cardEl.querySelector(".seg-quick-row");
  if (!row) return;
  row.innerHTML = "";

  // Always show Ignore button
  if (_ignorePersonId) {
    const ignBtn = document.createElement("button");
    ignBtn.className = "seg-quick-btn ignore";
    ignBtn.textContent = "Ignore";
    ignBtn.addEventListener("click", async () => {
      _setAssignInput(cardEl, _ignorePersonId, "Ignore");
      await handleTag(segmentId, _ignorePersonId, cardEl);
      _personFrequency.set(_ignorePersonId, (_personFrequency.get(_ignorePersonId) || 0) + 1);
    });
    row.appendChild(ignBtn);
  }

  // Quick-assign people buttons (top 4 by frequency)
  const quickPeople = _getQuickAssignPeople();
  const visible = quickPeople.slice(0, 4);
  const overflow = quickPeople.slice(4);

  for (const p of visible) {
    const btn = document.createElement("button");
    btn.className = "seg-quick-btn";
    btn.textContent = p.canonical_name;
    btn.title = p.canonical_name;
    btn.addEventListener("click", () => handleQuickAssign(segmentId, p.person_id, cardEl));
    row.appendChild(btn);
  }

  // "More" button with dropdown for overflow
  if (overflow.length > 0) {
    const moreWrap = document.createElement("div");
    moreWrap.style.position = "relative";
    moreWrap.style.display = "inline-flex";

    const moreBtn = document.createElement("button");
    moreBtn.className = "seg-quick-btn seg-quick-more";
    moreBtn.innerHTML = "More &#9662;";

    const dropdown = document.createElement("div");
    dropdown.className = "seg-quick-dropdown";
    dropdown.hidden = true;

    for (const p of overflow) {
      const item = document.createElement("button");
      item.className = "seg-quick-dropdown-item";
      item.textContent = p.canonical_name;
      item.title = p.canonical_name;
      item.addEventListener("click", () => {
        dropdown.hidden = true;
        handleQuickAssign(segmentId, p.person_id, cardEl);
      });
      dropdown.appendChild(item);
    }

    moreBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      // Close any other open quick-assign dropdowns
      document.querySelectorAll(".seg-quick-dropdown").forEach(d => { d.hidden = true; });
      dropdown.hidden = !dropdown.hidden;
    });

    moreWrap.appendChild(moreBtn);
    moreWrap.appendChild(dropdown);
    row.appendChild(moreWrap);

    // Close dropdown when clicking elsewhere
    const closeHandler = (e) => {
      if (!moreWrap.contains(e.target)) {
        dropdown.hidden = true;
        document.removeEventListener("click", closeHandler);
      }
    };
    moreBtn.addEventListener("click", () => {
      setTimeout(() => document.addEventListener("click", closeHandler), 0);
    });
  }
}

async function handleQuickAssign(segmentId, personId, cardEl) {
  // Set the autocomplete input to match
  const person = people.find(p => p.person_id === personId);
  _setAssignInput(cardEl, personId, person?.canonical_name || "");

  const seg = segments.find(s => s.segment_id === segmentId);
  const duration = seg ? (seg.end_time - seg.start_time) : 0;
  const overlap = seg ? (seg.overlap_ratio || 0) : 0;

  if (duration < MIN_VOICEPRINT_DURATION || overlap > MAX_OVERLAP_FOR_VOICEPRINT) {
    await handleTag(segmentId, personId, cardEl);
  } else {
    await handleConfirm(segmentId, personId, cardEl);
  }

  // Update frequency and refresh quick buttons on all visible cards
  _personFrequency.set(personId, (_personFrequency.get(personId) || 0) + 1);
  _refreshAllQuickButtons();
}

function _refreshAllQuickButtons() {
  const cards = transcriptList().querySelectorAll(".segment-card");
  for (const card of cards) {
    const segId = card.dataset.segmentId;
    if (segId) _renderQuickButtons(segId, card);
  }
}

// ── Inline text editing ───────────────────────────────────────────────────────

function startTextEdit(card, seg, textEl) {
  if (textEl.getAttribute("contenteditable") === "true") return;
  textEl.setAttribute("contenteditable", "true");
  textEl.classList.add("editing");
  textEl.focus();

  // Move cursor to end
  const range = document.createRange();
  range.selectNodeContents(textEl);
  range.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  const editControls = card.querySelector(".seg-edit-controls");
  editControls.classList.add("visible");

  // Ctrl+Enter to save, Escape to cancel
  textEl._keyHandler = e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      saveTextEdit(card, seg, textEl);
    } else if (e.key === "Escape") {
      cancelTextEdit(card, seg, textEl);
    }
  };
  textEl.addEventListener("keydown", textEl._keyHandler);
}

async function saveTextEdit(card, seg, textEl) {
  const newText = textEl.textContent.trim();
  if (!newText) { alert("Text cannot be empty."); return; }

  const saveBtn = card.querySelector(".seg-save-btn");
  saveBtn.textContent = "Saving…";
  saveBtn.disabled = true;

  try {
    await editSegmentText(seg.segment_id, newText);
    // Update local state
    const localSeg = segments.find(s => s.segment_id === seg.segment_id);
    if (localSeg) localSeg.text = newText;
    seg.text = newText;
  } catch (err) {
    alert(`Save failed: ${err.message}`);
    textEl.textContent = seg.text; // revert
  } finally {
    finishTextEdit(card, textEl);
    saveBtn.textContent = "Save";
    saveBtn.disabled = false;
  }
}

function cancelTextEdit(card, seg, textEl) {
  textEl.textContent = seg.text;
  finishTextEdit(card, textEl);
}

function finishTextEdit(card, textEl) {
  textEl.setAttribute("contenteditable", "false");
  textEl.classList.remove("editing");
  if (textEl._keyHandler) {
    textEl.removeEventListener("keydown", textEl._keyHandler);
    delete textEl._keyHandler;
  }
  const editControls = card.querySelector(".seg-edit-controls");
  editControls.classList.remove("visible");
}

// ── Confirm handler ───────────────────────────────────────────────────────────

async function handleConfirm(segmentId, personId, cardEl) {
  const btn = cardEl?.querySelector(".seg-confirm-btn");
  if (btn) { btn.textContent = "…"; btn.disabled = true; }

  try {
    await confirmAssignment(segmentId, personId);

    // Track as confirmed so polling won't overwrite this card
    _confirmedThisSession.add(segmentId);

    // Update the card in-place immediately
    const person = people.find(p => p.person_id === personId);
    if (cardEl && person) {
      cardEl.className = cardEl.className.replace(/conf-\w+/, "conf-verified");
      const badge = cardEl.querySelector(".seg-badge");
      if (badge) {
        badge.className = "seg-badge badge-verified";
        badge.textContent = "✓ verified";
      }
      const nameEl = cardEl.querySelector(".seg-speaker-name");
      if (nameEl) nameEl.textContent = person.canonical_name;
      if (btn) {
        btn.textContent = "✓";
        btn.classList.add("confirmed");
        btn.disabled = false;
      }
    }

    // Update local segment state
    const localSeg = segments.find(s => s.segment_id === segmentId);
    if (localSeg) {
      if (!localSeg.assignment) localSeg.assignment = {};
      localSeg.assignment.verified = true;
      localSeg.assignment.predicted_person_id = personId;
    }

    updateStats();

    // Background task is now running — show indicator and poll for updates
    // to OTHER unverified segments, but never overwrite confirmed ones
    showReprocessIndicator();

  } catch (err) {
    alert(`Failed to confirm: ${err.message}`);
    if (btn) { btn.textContent = "Confirm"; btn.disabled = false; }
  }
}

// ── Tag handler ──────────────────────────────────────────────────────────────

async function handleTag(segmentId, personId, cardEl) {
  const btn = cardEl?.querySelector(".seg-tag-btn");
  if (btn) { btn.textContent = "…"; btn.disabled = true; }

  try {
    await tagAssignment(segmentId, personId);

    // Track so polling won't overwrite
    _confirmedThisSession.add(segmentId);

    // Update the card in-place
    const person = people.find(p => p.person_id === personId);
    if (cardEl && person) {
      cardEl.className = cardEl.className.replace(/conf-\w+/, "conf-tagged");
      const badge = cardEl.querySelector(".seg-badge");
      if (badge) {
        badge.className = "seg-badge badge-tagged";
        badge.textContent = "tagged";
      }
      const nameEl = cardEl.querySelector(".seg-speaker-name");
      if (nameEl) nameEl.textContent = person.canonical_name;
      if (btn) {
        btn.textContent = "✦ Tagged";
        btn.classList.add("is-tagged");
        btn.disabled = false;
      }
    }

    // Update local segment state
    const localSeg = segments.find(s => s.segment_id === segmentId);
    if (localSeg) {
      if (!localSeg.assignment) localSeg.assignment = {};
      localSeg.assignment.tagged = true;
      localSeg.assignment.verified = false;
      localSeg.assignment.predicted_person_id = personId;
      localSeg.assignment.similarity_score = null;
    }

    updateStats();
  } catch (err) {
    alert(`Failed to tag: ${err.message}`);
    if (btn) { btn.textContent = "Tag"; btn.disabled = false; }
  }
}

// ── Stats bar ─────────────────────────────────────────────────────────────────

function updateStats() {
  const total    = segments.length;
  const verified = segments.filter(s => s.assignment?.verified).length;
  const tagged   = segments.filter(s => s.assignment?.tagged && !s.assignment?.verified).length;
  const unknown  = segments.filter(s => !s.assignment?.predicted_person_id).length;

  const el = document.getElementById("segment-stats");
  if (total === 0) { el.textContent = "—"; return; }
  const parts = [`${verified}/${total} verified`];
  if (tagged > 0) parts.push(`${tagged} tagged`);
  parts.push(`${unknown} unknown`);
  el.textContent = parts.join(" · ");
}

// ── Reprocess indicator ───────────────────────────────────────────────────────

function showReprocessIndicator() {
  const el = document.getElementById("reprocess-indicator");
  el.classList.add("visible");

  let attempts = 0;
  const MAX = 6;
  const timer = setInterval(async () => {
    attempts++;
    // Skip render if user is interacting with a dropdown or editing text
    if (isUserInteracting()) return;
    const fresh = await fetchSegments();
    if (fresh) {
      // Merge: keep confirmed-this-session cards, update everything else
      segments = fresh.map(s => {
        if (_confirmedThisSession.has(s.segment_id)) {
          return segments.find(x => x.segment_id === s.segment_id) || s;
        }
        return s;
      });
      renderTranscript();
      updateStats();
    }
    if (attempts >= MAX) {
      clearInterval(timer);
      el.classList.remove("visible");
    }
  }, 5000);
}

// ── Video synchronisation ─────────────────────────────────────────────────────

function setupVideoEvents() {
  const v = video();
  if (!v) return;

  v.addEventListener("timeupdate", () => {
    syncActiveCard(v.currentTime, true);
    const cur = document.getElementById("current-time-display");
    const dur = document.getElementById("duration-display");
    if (cur) cur.textContent = fmtTime(v.currentTime);
    if (!v.duration) return;
    if (dur && dur.textContent === "—") dur.textContent = fmtTime(v.duration);
  });

  v.addEventListener("loadedmetadata", () => {
    const dur = document.getElementById("duration-display");
    if (dur) dur.textContent = fmtTime(v.duration);
  });
}

function syncActiveCard(t, scrollIntoView) {
  if (segments.length === 0) return;

  const idx = segments.findIndex(s => t >= s.start_time && t <= s.end_time);
  if (idx === activeIndex) return;

  if (activeIndex >= 0) {
    const old = transcriptList().querySelector(`[data-idx="${activeIndex}"]`);
    if (old) old.classList.remove("is-active");
  }

  activeIndex = idx;

  if (idx < 0) {
    document.getElementById("active-speaker-label").textContent = "";
    return;
  }

  const card = transcriptList().querySelector(`[data-idx="${idx}"]`);
  if (card) {
    card.classList.add("is-active");
    if (scrollIntoView) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  const seg    = segments[idx];
  const assign = seg.assignment;
  const person = assign?.predicted_person_id
    ? people.find(p => p.person_id === assign.predicted_person_id)
    : null;
  document.getElementById("active-speaker-label").textContent =
    person?.canonical_name ?? seg.raw_speaker_label ?? "";
}

// ── Deep-link: seek to first segment for a speaker ───────────────────────────

function seekToSpeakerFromURL() {
  const params = new URLSearchParams(window.location.search);
  const speakerId = params.get("speaker");
  if (!speakerId || segments.length === 0) return;

  // Find the first segment assigned to this speaker
  const seg = segments.find(s =>
    s.assignment && s.assignment.predicted_person_id === speakerId
  );
  if (!seg) return;

  const v = video();
  if (!v) return;

  // Wait for the video to be ready enough to seek
  function doSeek() {
    v.currentTime = seg.start_time;
    v.pause();
    syncActiveCard(seg.start_time, false);
    // Scroll the card to the top of the transcript panel
    const idx = segments.indexOf(seg);
    const card = transcriptList().querySelector(`[data-idx="${idx}"]`);
    if (card) card.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  if (v.readyState >= 1) {
    doSeek();
  } else {
    v.addEventListener("loadedmetadata", doSeek, { once: true });
  }
}

// ── Pipeline polling ──────────────────────────────────────────────────────────

async function checkPipelineAndPoll() {
  const status   = await fetchStatus();
  const hasMedia = await fetchHasMedia();

  if (!hasMedia) {
    document.getElementById("upload-bar").hidden = false;
    return;
  }

  setVideoSource();

  if (!status || status.status === "processing" || status.segment_count === 0) {
    updateBannerProgress(status);
    showPipelineBanner("Pipeline processing…");
    startPipelinePoll();
    return;
  }

  hidePipelineBanner();
}

function startPipelinePoll() {
  if (pollTimer) clearInterval(pollTimer);
  let nullCount = 0;
  pollTimer = setInterval(async () => {
    // Skip render if user is interacting with a dropdown or editing text
    if (isUserInteracting()) return;
    const s = await fetchStatus();
    if (!s) {
      // Meeting or media may have been deleted — stop after 3 consecutive nulls
      nullCount++;
      if (nullCount >= 3) {
        clearInterval(pollTimer);
        pollTimer = null;
        hidePipelineBanner();
      }
      return;
    }
    nullCount = 0;
    updateBannerProgress(s);
    if (s.status === "complete" || s.segment_count > 0) {
      // Load segments if we have any (show transcript as soon as transcription is done)
      if (s.segment_count > 0) await loadSegments();
      if (s.status === "complete") {
        clearInterval(pollTimer);
        pollTimer = null;
        hidePipelineBanner();
      }
    }
  }, 4000);
}

function updateBannerProgress(status) {
  if (!status) return;
  const stageEl  = document.getElementById("banner-stage");
  const pctEl    = document.getElementById("banner-pct");
  const fillEl   = document.getElementById("banner-bar-fill");
  const detailEl = document.getElementById("banner-detail");
  const progressEl = document.getElementById("banner-progress");

  if (!stageEl) return;

  const stage  = status.stage || "";
  const pct    = status.progress_pct ?? 0;
  const detail = status.detail || "";

  progressEl.style.display = stage ? "block" : "none";
  stageEl.textContent  = stage;
  pctEl.textContent    = pct > 0 ? `${pct}%` : "";
  detailEl.textContent = detail;

  if (pct > 0) {
    fillEl.classList.remove("indeterminate");
    fillEl.style.width = `${pct}%`;
  } else {
    fillEl.classList.add("indeterminate");
    fillEl.style.width = "35%";
  }
}

async function fetchStatus() {
  try {
    const r = await fetch(`/api/media/${meetingId}/status`);
    return r.ok ? r.json() : null;
  } catch { return null; }
}

async function fetchHasMedia() {
  try {
    const r = await fetch(`/api/media/${meetingId}`);
    if (!r.ok) return false;
    const files = await r.json();
    return files.some(f => f.file_type === "video" || f.file_type === "audio");
  } catch { return false; }
}

function setVideoSource() {
  const v = video();
  if (!v || v.src) return;
  v.src = `/media/${meetingId}/video`;
}

function showPipelineBanner(msg) {
  const banner = document.getElementById("pipeline-banner");
  document.getElementById("pipeline-banner-text").textContent = msg;
  banner.hidden = false;
}

function hidePipelineBanner() {
  document.getElementById("pipeline-banner").hidden = true;
}

// ── Controls setup ────────────────────────────────────────────────────────────

function setupControls() {
  document.getElementById("filter-select")
    .addEventListener("change", loadSegments);

  document.getElementById("reprocess-btn")
    .addEventListener("click", async () => {
      const btn = document.getElementById("reprocess-btn");
      const hasSegments = segments.length > 0;

      if (!hasSegments) {
        // First-time process: trigger the pipeline
        if (!confirm(`Start processing "${meeting?.title || "this meeting"}"? This will use GPU resources.`)) return;
        btn.textContent = "↻ Starting…";
        btn.disabled = true;
        try {
          const r = await fetch(`/api/media/${meetingId}/process`, { method: "POST" });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${r.status}`);
          }
          showPipelineBanner("Pipeline processing…");
          startPipelinePoll();
        } catch (err) {
          alert(`Process failed: ${err.message}`);
        } finally {
          updateProcessButton();
          btn.disabled = false;
        }
      } else {
        // Re-run voiceprint matching on unverified segments
        btn.textContent = "↻ Running…";
        btn.disabled = true;
        try {
          await reprocessAll();
          showReprocessIndicator();
        } catch (err) {
          alert(`Reprocess failed: ${err.message}`);
        } finally {
          updateProcessButton();
          btn.disabled = false;
        }
      }
    });
}

function updateProcessButton() {
  const btn = document.getElementById("reprocess-btn");
  if (!btn) return;
  if (segments.length > 0) {
    btn.textContent = "↻ Reprocess";
    btn.title = "Re-run voiceprint matching on all unverified segments";
  } else {
    btn.textContent = "▶ Process";
    btn.title = "Start the transcription pipeline for this meeting";
  }
}

// ── Export dropdown ───────────────────────────────────────────────────────────

function setupExportDropdown() {
  const btn  = document.getElementById("export-btn");
  const menu = document.getElementById("export-menu");

  btn.addEventListener("click", e => {
    e.stopPropagation();
    menu.classList.toggle("open");
  });

  document.addEventListener("click", () => menu.classList.remove("open"));

  document.getElementById("export-srt").addEventListener("click", () => {
    triggerExport("srt");
    menu.classList.remove("open");
  });
  document.getElementById("export-txt").addEventListener("click", () => {
    triggerExport("txt");
    menu.classList.remove("open");
  });
  document.getElementById("export-json").addEventListener("click", () => {
    triggerExport("json");
    menu.classList.remove("open");
  });
  document.getElementById("export-pdf").addEventListener("click", () => {
    triggerExport("pdf");
    menu.classList.remove("open");
  });
}

function triggerExport(format) {
  const url = `/api/segments/${meetingId}/export?format=${format}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ── Rerun pipeline dialog ─────────────────────────────────────────────────────

function setupRerunDialog() {
  const dialog    = document.getElementById("rerun-dialog");
  const rerunBtn  = document.getElementById("rerun-btn");
  const cancelBtn = document.getElementById("cancel-rerun-btn");
  const closeBtn  = document.getElementById("close-rerun-dialog");
  const confirmBtn = document.getElementById("confirm-rerun-btn");

  rerunBtn.addEventListener("click", () => {
    dialog.showModal();
    document.getElementById("overlay").classList.add("active");
  });

  const closeDialog = () => {
    dialog.close();
    document.getElementById("overlay").classList.remove("active");
  };

  cancelBtn.addEventListener("click", closeDialog);
  closeBtn.addEventListener("click", closeDialog);

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.textContent = "Starting…";
    confirmBtn.disabled = true;
    try {
      const r = await fetch(`/api/media/${meetingId}/rerun`, { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${r.status}`);
      }
      closeDialog();
      // Clear local state
      segments = [];
      _confirmedThisSession.clear();
      renderTranscript();
      updateStats();
      // Reload video source (video file is preserved)
      const v = video();
      if (v) { v.removeAttribute("src"); v.load(); }
      setVideoSource();
      // Show banner and start polling
      showPipelineBanner("Re-running pipeline…");
      startPipelinePoll();
    } catch (err) {
      alert(`Re-run failed: ${err.message}`);
    } finally {
      confirmBtn.textContent = "Yes, Re-run";
      confirmBtn.disabled = false;
    }
  });
}

// ── Video upload ──────────────────────────────────────────────────────────────

function setupDocumentUploads() {
  const videoInput = document.getElementById("video-upload-input");
  const videoBtn   = document.getElementById("video-upload-btn");
  if (videoBtn) {
    videoBtn.addEventListener("click", () => {
      const f = videoInput?.files?.[0];
      if (!f) { alert("Select a video file first."); return; }
      uploadVideo(f, videoBtn);
    });
  }

  setupBarPdfButton("agenda-upload-input",  "agenda-upload-btn",  "agenda");
  setupBarPdfButton("minutes-upload-input", "minutes-upload-btn", "minutes");

  document.querySelectorAll(".doc-file-input").forEach(input => {
    input.addEventListener("change", async () => {
      const f = input.files?.[0];
      if (!f) return;
      const docType = input.dataset.docType;
      await uploadPdf(f, docType);
      input.value = "";
      await loadDocuments();
    });
  });
}

function setupBarPdfButton(inputId, btnId, docType) {
  const input = document.getElementById(inputId);
  const btn   = document.getElementById(btnId);
  if (!btn || !input) return;
  btn.addEventListener("click", async () => {
    const f = input.files?.[0];
    if (!f) { alert(`Select a ${docType} PDF first.`); return; }
    btn.textContent = "Uploading…";
    btn.disabled = true;
    try {
      await uploadPdf(f, docType);
      await loadDocuments();
    } finally {
      btn.textContent = "Upload";
      btn.disabled = false;
    }
  });
}

async function uploadVideo(file, btn) {
  btn.textContent = "Uploading…";
  btn.disabled = true;

  const fd = new FormData();
  fd.append("file", file);

  try {
    const r = await fetch(`/api/media/${meetingId}/upload`, {
      method: "POST",
      body: fd,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    document.getElementById("upload-bar").hidden = true;
    showPipelineBanner("Processing started… transcript will appear when ready.");
    setVideoSource();
    startPipelinePoll();
  } catch (err) {
    alert(`Upload failed: ${err.message}`);
  } finally {
    btn.textContent = "Upload & Process";
    btn.disabled = false;
  }
}

async function uploadPdf(file, docType) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("document_type", docType);

  const r = await fetch(`/api/documents/${meetingId}/upload`, {
    method: "POST",
    body: fd,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// ── Documents render ──────────────────────────────────────────────────────────

function renderDocuments() {
  const list = document.getElementById("doc-list");
  const tabBar = document.getElementById("doc-tabs");
  const viewer = document.getElementById("doc-viewer");
  const frame = document.getElementById("doc-viewer-frame");

  // Reset tabs — keep only the "Documents" list tab
  tabBar.innerHTML = '<button class="doc-tab active" data-doc-tab="list">Documents</button>';

  if (documents.length === 0) {
    list.innerHTML = `<div class="doc-empty">No documents uploaded.</div>`;
    list.style.display = "";
    viewer.style.display = "none";
    return;
  }

  // Build list view
  list.innerHTML = "";
  documents.forEach(doc => {
    const item = document.createElement("div");
    item.className = "doc-item";
    const hasText  = doc.ocr_text && doc.ocr_text.trim().length > 0;
    const filename = doc.file_path.split(/[/\\]/).pop();

    const DOC_LABELS = { "agenda": "Agenda Overview", "minutes": "Minutes", "packet": "Agenda Packet" };
    const typeLabel = DOC_LABELS[doc.document_type] || doc.document_type.charAt(0).toUpperCase() + doc.document_type.slice(1);

    item.innerHTML = `
      <span class="doc-item-type">${esc(typeLabel)}</span>
      <span style="font-size:11px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
        ${esc(filename)}
      </span>
      <span class="doc-item-status ${hasText ? "ready" : ""}">
        ${hasText ? `\u2713 ${doc.ocr_text.length.toLocaleString()} chars` : "processing\u2026"}
      </span>
    `;
    list.appendChild(item);
  });

  // Add a tab for each document (agenda overview, agenda packet, minutes, etc.)
  const DOC_TAB_LABELS = { "agenda": "Agenda Overview", "minutes": "Minutes", "packet": "Agenda Packet" };
  documents.forEach(doc => {
    const label = DOC_TAB_LABELS[doc.document_type] || doc.document_type.charAt(0).toUpperCase() + doc.document_type.slice(1);
    const tab = document.createElement("button");
    tab.className = "doc-tab";
    tab.dataset.docTab = doc.document_id;
    tab.textContent = label;
    tabBar.appendChild(tab);
  });

  // Tab click handler
  tabBar.addEventListener("click", e => {
    const tab = e.target.closest(".doc-tab");
    if (!tab) return;
    tabBar.querySelectorAll(".doc-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");

    const tabId = tab.dataset.docTab;
    if (tabId === "list") {
      list.style.display = "";
      viewer.style.display = "none";
      frame.src = "";
    } else {
      list.style.display = "none";
      viewer.style.display = "";
      frame.src = `/api/documents/${meetingId}/${tabId}/file`;
    }
  });
}

// ── New person dialog ─────────────────────────────────────────────────────────

function setupPersonDialog() {
  const dialog = document.getElementById("new-person-dialog");
  const input  = document.getElementById("new-person-name");

  document.getElementById("close-person-dialog")
    .addEventListener("click", () => dialog.close());
  document.getElementById("cancel-person-btn")
    .addEventListener("click", () => dialog.close());
  document.getElementById("overlay")
    .addEventListener("click", () => {
      dialog.close();
      document.getElementById("rerun-dialog")?.close();
    });

  input.addEventListener("keydown", e => {
    if (e.key === "Enter") handleCreatePerson();
  });

  document.getElementById("create-person-confirm-btn")
    .addEventListener("click", handleCreatePerson);
}

function openPersonDialog() {
  document.getElementById("new-person-name").value = "";
  document.getElementById("new-person-dialog").showModal();
}

async function handleCreatePerson() {
  const name = document.getElementById("new-person-name").value.trim();
  if (!name) { alert("Enter a name."); return; }

  const btn = document.getElementById("create-person-confirm-btn");
  btn.textContent = "Creating…";
  btn.disabled = true;

  try {
    let person = people.find(p =>
      p.canonical_name.toLowerCase() === name.toLowerCase()
    );

    if (!person) {
      person = await createPerson(name);
      people.push(person);
    }

    document.getElementById("new-person-dialog").close();

    if (pendingSegmentId) {
      const cardEl = transcriptList().querySelector(`[data-segment-id="${pendingSegmentId}"]`);
      await handleConfirm(pendingSegmentId, person.person_id, cardEl);
      pendingSegmentId = null;
    } else {
      await loadSegments();
    }
  } catch (err) {
    alert(`Failed: ${err.message}`);
  } finally {
    btn.textContent = "Create & Assign";
    btn.disabled = false;
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function fmtTime(seconds) {
  if (seconds == null || isNaN(seconds)) return "—";
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

// ── Tags on Segments ────────────────────────────────────────────────────────

async function loadSegmentTags(segmentId) {
  try {
    const r = await fetch(`/api/tags/for-content?content_type=meeting_segment&content_id=${segmentId}`);
    if (!r.ok) return;
    const assignments = await r.json();
    const container = document.getElementById(`seg-tags-${segmentId}`);
    if (!container || assignments.length === 0) return;

    container.innerHTML = "";
    assignments.forEach(a => {
      const pill = document.createElement("span");
      const sourceClass = a.source === "confirmed" ? "confirmed" : a.source === "llm" ? "llm" : "";
      pill.className = `tag-pill ${sourceClass}`;
      pill.innerHTML = `
        ${esc(a.tag?.name || "tag")}
        <button class="pill-action" title="Confirm" onclick="event.stopPropagation(); confirmSegTag('${a.assignment_id}', '${segmentId}')">&#x2713;</button>
        <button class="pill-action" title="Deny" onclick="event.stopPropagation(); denySegTag('${a.tag_id}','meeting_segment','${segmentId}')">&#x2715;</button>
      `;
      container.appendChild(pill);
    });
  } catch { /* ignore */ }
}

async function confirmSegTag(assignmentId, segmentId) {
  await fetch(`/api/tags/assignments/${assignmentId}/confirm`, { method: "POST" });
  loadSegmentTags(segmentId);
}

async function denySegTag(tagId, contentType, contentId) {
  await fetch("/api/tags/deny", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tag_id: tagId, content_type: contentType, content_id: contentId }),
  });
  loadSegmentTags(contentId);
}

// ── Clip Builder ──────────────────────────────────────────────────────────────

let clipStart = 0;
let clipEnd = 0;
let clipPreviewTimer = null;

function setupClipBuilder() {
  const toggleBtn = document.getElementById("clip-toggle-btn");
  const closeBtn  = document.getElementById("clip-close-btn");
  const panel     = document.getElementById("clip-builder");

  toggleBtn.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
  });
  closeBtn.addEventListener("click", () => {
    panel.hidden = true;
    stopClipPreview();
  });

  // Set start/end from current video time
  document.getElementById("clip-set-start").addEventListener("click", () => {
    const v = video();
    if (v) {
      clipStart = v.currentTime;
      document.getElementById("clip-start-input").value = fmtClipTime(clipStart);
      updateClipDuration();
    }
  });
  document.getElementById("clip-set-end").addEventListener("click", () => {
    const v = video();
    if (v) {
      clipEnd = v.currentTime;
      document.getElementById("clip-end-input").value = fmtClipTime(clipEnd);
      updateClipDuration();
    }
  });

  // Manual time input
  document.getElementById("clip-start-input").addEventListener("change", e => {
    clipStart = parseClipTime(e.target.value);
    e.target.value = fmtClipTime(clipStart);
    updateClipDuration();
  });
  document.getElementById("clip-end-input").addEventListener("change", e => {
    clipEnd = parseClipTime(e.target.value);
    e.target.value = fmtClipTime(clipEnd);
    updateClipDuration();
  });

  // Adjust buttons
  document.querySelectorAll(".clip-adjust-buttons button").forEach(btn => {
    btn.addEventListener("click", () => {
      const which = btn.dataset.which;
      const delta = parseFloat(btn.dataset.delta);
      adjustClipTime(which, delta);
    });
  });

  // Preview & loop
  document.getElementById("clip-preview-btn").addEventListener("click", previewClip);

  // Export
  document.getElementById("clip-export-btn").addEventListener("click", createClip);

  // Check URL params for clip deep-link
  seekToTimeFromURL();
}

function fmtClipTime(sec) {
  if (sec == null || isNaN(sec) || sec < 0) sec = 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const ms = Math.round((sec % 1) * 1000);
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

function parseClipTime(str) {
  if (!str) return 0;
  const parts = str.split(":");
  let sec = 0;
  if (parts.length === 3) {
    sec = parseFloat(parts[0]) * 3600 + parseFloat(parts[1]) * 60 + parseFloat(parts[2]);
  } else if (parts.length === 2) {
    sec = parseFloat(parts[0]) * 60 + parseFloat(parts[1]);
  } else {
    sec = parseFloat(parts[0]) || 0;
  }
  return isNaN(sec) ? 0 : Math.max(0, sec);
}

function updateClipDuration() {
  const dur = clipEnd - clipStart;
  const el = document.getElementById("clip-duration-display");
  if (dur <= 0) {
    el.textContent = "0:00";
    el.style.color = "var(--accent-red)";
  } else {
    el.textContent = fmtTime(dur);
    el.style.color = "";
  }
}

function adjustClipTime(which, delta) {
  const v = video();
  const maxDur = v?.duration || Infinity;

  if (which === "start") {
    clipStart = Math.max(0, Math.min(clipStart + delta, maxDur));
    document.getElementById("clip-start-input").value = fmtClipTime(clipStart);
    if (v) v.currentTime = clipStart;
  } else {
    clipEnd = Math.max(0, Math.min(clipEnd + delta, maxDur));
    document.getElementById("clip-end-input").value = fmtClipTime(clipEnd);
    if (v) v.currentTime = clipEnd;
  }
  updateClipDuration();
}

function previewClip() {
  const v = video();
  if (!v || clipEnd <= clipStart) return;

  stopClipPreview();
  v.currentTime = clipStart;
  v.play().catch(() => {});

  const loopCheck = document.getElementById("clip-loop-check");

  function onTimeUpdate() {
    if (v.currentTime >= clipEnd) {
      if (loopCheck.checked) {
        v.currentTime = clipStart;
      } else {
        v.pause();
        v.removeEventListener("timeupdate", onTimeUpdate);
      }
    }
  }

  v.addEventListener("timeupdate", onTimeUpdate);
  // Store so we can clean up
  clipPreviewTimer = () => v.removeEventListener("timeupdate", onTimeUpdate);
}

function stopClipPreview() {
  if (clipPreviewTimer) {
    clipPreviewTimer();
    clipPreviewTimer = null;
  }
}

async function createClip() {
  if (clipEnd <= clipStart) {
    alert("End time must be after start time.");
    return;
  }

  const btn = document.getElementById("clip-export-btn");
  const statusEl = document.getElementById("clip-status");
  btn.disabled = true;
  btn.textContent = "Exporting...";
  statusEl.textContent = "creating...";
  statusEl.className = "clip-status";

  try {
    const title = document.getElementById("clip-title-input").value.trim() || "Untitled clip";
    const notes = document.getElementById("clip-notes-input").value.trim() || null;

    const r = await fetch("/api/clips/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_type: "meeting",
        source_id: meetingId,
        start_time: clipStart,
        end_time: clipEnd,
        title,
        notes,
      }),
    });

    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }

    const clip = await r.json();
    pollClipStatus(clip.clip_id, statusEl, btn);

  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    statusEl.className = "clip-status clip-status-error";
    btn.disabled = false;
    btn.textContent = "Export Clip";
  }
}

async function pollClipStatus(clipId, statusEl, btn) {
  let attempts = 0;
  const MAX = 660; // 660 * 1s = 11 minutes max (matches FFmpeg 600s timeout)

  // Show progress bar
  statusEl.innerHTML = `
    <div class="clip-progress">
      <div class="clip-progress-track">
        <div class="clip-progress-fill" id="clip-progress-fill" style="width:0%"></div>
      </div>
      <span class="clip-progress-pct" id="clip-progress-pct">0%</span>
    </div>`;
  statusEl.className = "clip-status";

  const timer = setInterval(async () => {
    attempts++;
    try {
      const r = await fetch(`/api/clips/${clipId}/progress`);
      if (!r.ok) throw new Error("Poll failed");
      const data = await r.json();

      if (data.status === "ready") {
        clearInterval(timer);
        statusEl.innerHTML = `<a href="/api/clips/${clipId}/download" class="clip-download-link">&#8615; Download</a>`;
        statusEl.className = "clip-status clip-status-ready";
        btn.disabled = false;
        btn.textContent = "Export Clip";
      } else if (data.status === "error") {
        clearInterval(timer);
        statusEl.textContent = `Error: ${data.error || "export failed"}`;
        statusEl.className = "clip-status clip-status-error";
        btn.disabled = false;
        btn.textContent = "Export Clip";
      } else {
        const fill = document.getElementById("clip-progress-fill");
        const pctEl = document.getElementById("clip-progress-pct");
        if (fill) fill.style.width = `${data.pct || 0}%`;
        if (pctEl) pctEl.textContent = `${data.pct || 0}%`;
      }
    } catch { /* ignore */ }

    if (attempts >= MAX) {
      clearInterval(timer);
      statusEl.textContent = "timed out";
      btn.disabled = false;
      btn.textContent = "Export Clip";
    }
  }, 1000);
}

function seekToTimeFromURL() {
  const params = new URLSearchParams(window.location.search);
  const t = params.get("t");
  const clipId = params.get("clip");

  if (clipId) {
    // Deep-link from clip library — open builder and pre-fill
    fetch(`/api/clips/${clipId}`)
      .then(r => r.ok ? r.json() : null)
      .then(clip => {
        if (!clip) return;
        clipStart = clip.start_time;
        clipEnd = clip.end_time;
        document.getElementById("clip-start-input").value = fmtClipTime(clipStart);
        document.getElementById("clip-end-input").value = fmtClipTime(clipEnd);
        document.getElementById("clip-title-input").value = clip.title || "";
        document.getElementById("clip-notes-input").value = clip.notes || "";
        updateClipDuration();
        document.getElementById("clip-builder").hidden = false;

        const v = video();
        if (v) {
          const doSeek = () => { v.currentTime = clipStart; };
          if (v.readyState >= 1) doSeek();
          else v.addEventListener("loadedmetadata", doSeek, { once: true });
        }
      })
      .catch(() => {});
  } else if (t) {
    const seconds = parseFloat(t);
    if (!isNaN(seconds)) {
      const v = video();
      if (v) {
        const doSeek = () => { v.currentTime = seconds; };
        if (v.readyState >= 1) doSeek();
        else v.addEventListener("loadedmetadata", doSeek, { once: true });
      }
    }
  }
}

// ── Summary Panel ─────────────────────────────────────────────────────────────

let _activeSummaryTab = "short";

function setupSummaryPanel() {
  // Tab switching
  document.querySelectorAll(".summary-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      _activeSummaryTab = tab.dataset.summaryTab;
      document.querySelectorAll(".summary-tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById("summary-short-text").style.display = _activeSummaryTab === "short" ? "" : "none";
      document.getElementById("summary-long-text").style.display  = _activeSummaryTab === "long"  ? "" : "none";
    });
  });

  // File upload handlers
  document.getElementById("summary-upload-short").addEventListener("change", e => {
    const f = e.target.files?.[0];
    if (f) uploadSummaryFile(f, "short");
    e.target.value = "";
  });
  document.getElementById("summary-upload-long").addEventListener("change", e => {
    const f = e.target.files?.[0];
    if (f) uploadSummaryFile(f, "long");
    e.target.value = "";
  });

  // Edit button
  document.getElementById("summary-edit-btn").addEventListener("click", () => {
    const editor = document.getElementById("summary-editor");
    const textarea = document.getElementById("summary-editor-textarea");
    const typeSelect = document.getElementById("summary-editor-type");
    editor.style.display = editor.style.display === "none" ? "" : "none";
    if (editor.style.display !== "none") {
      typeSelect.value = _activeSummaryTab;
      const current = _activeSummaryTab === "short" ? meeting?.summary_short : meeting?.summary_long;
      textarea.value = current || "";
      textarea.focus();
    }
  });

  // Save button
  document.getElementById("summary-save-btn").addEventListener("click", async () => {
    const textarea = document.getElementById("summary-editor-textarea");
    const typeSelect = document.getElementById("summary-editor-type");
    const summaryType = typeSelect.value;
    const text = textarea.value;

    const btn = document.getElementById("summary-save-btn");
    btn.textContent = "Saving…";
    btn.disabled = true;
    try {
      const body = {};
      body[summaryType === "short" ? "summary_short" : "summary_long"] = text;
      const r = await fetch(`/api/meetings/${meetingId}/summary`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      meeting.summary_short = data.summary_short;
      meeting.summary_long = data.summary_long;
      renderSummaries();
      document.getElementById("summary-editor").style.display = "none";
    } catch (err) {
      alert(`Save failed: ${err.message}`);
    } finally {
      btn.textContent = "Save";
      btn.disabled = false;
    }
  });

  // Cancel button
  document.getElementById("summary-cancel-btn").addEventListener("click", () => {
    document.getElementById("summary-editor").style.display = "none";
  });
}

async function uploadSummaryFile(file, summaryType) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch(`/api/meetings/${meetingId}/summary/upload?summary_type=${summaryType}`, {
      method: "POST",
      body: fd,
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    // Reload meeting to get updated summaries
    await loadMeeting();
    renderSummaries();
  } catch (err) {
    alert(`Upload failed: ${err.message}`);
  }
}

function renderSummaries() {
  const shortEl = document.getElementById("summary-short-text");
  const longEl  = document.getElementById("summary-long-text");

  if (meeting?.summary_short) {
    shortEl.textContent = meeting.summary_short;
    shortEl.classList.remove("empty");
  } else {
    shortEl.textContent = "No short summary yet.";
    shortEl.classList.add("empty");
  }

  if (meeting?.summary_long) {
    // Render long summary as preformatted text (preserves markdown structure)
    longEl.innerHTML = "";
    const pre = document.createElement("pre");
    pre.style.whiteSpace = "pre-wrap";
    pre.style.fontFamily = "inherit";
    pre.style.margin = "0";
    pre.textContent = meeting.summary_long;
    longEl.appendChild(pre);
    longEl.classList.remove("empty");
  } else {
    longEl.textContent = "No long summary yet.";
    longEl.classList.add("empty");
  }

  // Show the active tab
  shortEl.style.display = _activeSummaryTab === "short" ? "" : "none";
  longEl.style.display  = _activeSummaryTab === "long"  ? "" : "none";
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
