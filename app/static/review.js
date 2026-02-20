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
  await loadDocuments();
  await loadSegments();

  setupVideoEvents();
  setupControls();
  setupDocumentUploads();
  setupPersonDialog();
  setupExportDropdown();
  setupRerunDialog();

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
    document.getElementById("hdr-governing-body").textContent = meeting.governing_body;
    document.getElementById("hdr-title").textContent = meeting.title;
    document.title = `${meeting.title} — Civic Media`;
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
    <div class="seg-controls" onclick="event.stopPropagation()">
      <select class="seg-select" data-seg="${seg.segment_id}">
        <option value="">— Assign —</option>
        ${people.map(p => `
          <option value="${p.person_id}" ${p.person_id === personId ? "selected" : ""}>
            ${esc(p.canonical_name)}
          </option>`).join("")}
      </select>
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
  `;

  // Seek on card body click
  card.addEventListener("click", e => {
    if (e.target.closest(".seg-controls")) return;
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
    const sel = card.querySelector(".seg-select");
    if (!sel.value) { alert("Select a speaker first."); return; }
    await handleTag(seg.segment_id, sel.value, card);
  });

  // Confirm button
  card.querySelector(".seg-confirm-btn").addEventListener("click", async () => {
    const sel = card.querySelector(".seg-select");
    if (!sel.value) { alert("Select a speaker first."); return; }
    await handleConfirm(seg.segment_id, sel.value, card);
  });

  // New person button
  card.querySelector(".seg-new-btn").addEventListener("click", () => {
    pendingSegmentId = seg.segment_id;
    openPersonDialog();
  });

  return card;
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
      btn.textContent = "↻ Running…";
      btn.disabled = true;
      try {
        await reprocessAll();
        showReprocessIndicator();
      } catch (err) {
        alert(`Reprocess failed: ${err.message}`);
      } finally {
        btn.textContent = "↻ Reprocess";
        btn.disabled = false;
      }
    });
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

  if (documents.length === 0) {
    list.innerHTML = `<div class="doc-empty">No documents uploaded.</div>`;
    return;
  }

  list.innerHTML = "";
  documents.forEach(doc => {
    const item = document.createElement("div");
    item.className = "doc-item";
    const hasText  = doc.ocr_text && doc.ocr_text.trim().length > 0;
    const filename = doc.file_path.split("/").pop();

    item.innerHTML = `
      <span class="doc-item-type">${esc(doc.document_type)}</span>
      <span style="font-size:11px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
        ${esc(filename)}
      </span>
      <span class="doc-item-status ${hasText ? "ready" : ""}">
        ${hasText ? `✓ ${doc.ocr_text.length.toLocaleString()} chars` : "processing…"}
      </span>
    `;
    list.appendChild(item);
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

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
