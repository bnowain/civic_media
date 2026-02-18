/**
 * review.js — Side-by-side video review interface.
 *
 * Responsibilities:
 *   - Load meeting, segments, people from API.
 *   - Render a scrollable transcript beside the video.
 *   - Sync video playback position → active transcript card highlight.
 *   - Clicking a segment card seeks the video.
 *   - Confirm/correct speaker assignments → voiceprint learning loop.
 *   - Upload video, agenda, minutes PDFs.
 *   - Poll for pipeline completion with progress bar.
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
let reprocessPollTimer = null;
let pendingSegmentId = null;   // for new-person dialog

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

  checkPipelineAndPoll();
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function loadMeeting() {
  try {
    const r = await fetch(`/api/meetings/${meetingId}`);
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

async function loadSegments() {
  const filter = document.getElementById("filter-select").value;
  const url = `/api/segments/${meetingId}${filter ? `?filter=${filter}` : ""}`;
  try {
    const r = await fetch(url);
    if (!r.ok) return;
    segments = await r.json();
    renderTranscript();
    updateStats();
  } catch (err) {
    console.error("loadSegments:", err);
  }
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

  segments.forEach((seg, idx) => {
    const card = buildSegmentCard(seg, idx);
    list.appendChild(card);
  });

  list.scrollTop = scrollTop;
  activeIndex = -1;
  syncActiveCard(video()?.currentTime ?? 0, false);
}

function buildSegmentCard(seg, idx) {
  const assign   = seg.assignment;
  const verified = assign?.verified ?? false;
  const score    = assign?.similarity_score ?? null;
  const personId = assign?.predicted_person_id ?? null;

  const confClass = verified ? "verified"
    : score === null          ? "unknown"
    : score >= 0.92           ? "high"
    : score >= 0.75           ? "medium"
    : "unknown";

  const person = people.find(p => p.person_id === personId);
  const speakerName = person?.canonical_name ?? seg.raw_speaker_label ?? "Unknown";

  const scoreStr = score !== null ? `${Math.round(score * 100)}%` : "—";
  const badgeText = verified ? "✓ verified"
    : confClass === "unknown" ? "unknown"
    : `${confClass} ${scoreStr}`;

  const card = document.createElement("div");
  card.className = `segment-card conf-${confClass}`;
  card.dataset.segmentId = seg.segment_id;
  card.dataset.idx       = idx;
  card.dataset.start     = seg.start_time;
  card.dataset.end       = seg.end_time;

  card.innerHTML = `
    <div class="seg-meta">
      <span class="seg-time">${fmtTime(seg.start_time)}</span>
      <span class="seg-badge badge-${confClass}">${esc(badgeText)}</span>
      <span class="seg-speaker-name">${esc(speakerName)}</span>
    </div>
    <div class="seg-text">${esc(seg.text)}</div>
    <div class="seg-controls" onclick="event.stopPropagation()">
      <select class="seg-select" data-seg="${seg.segment_id}">
        <option value="">— Assign —</option>
        ${people.map(p => `
          <option value="${p.person_id}" ${p.person_id === personId ? "selected" : ""}>
            ${esc(p.canonical_name)}
          </option>`).join("")}
      </select>
      <button class="seg-confirm-btn ${verified ? "confirmed" : ""}"
              data-seg="${seg.segment_id}">
        ${verified ? "✓" : "Confirm"}
      </button>
      <button class="seg-new-btn" data-seg="${seg.segment_id}">+ New</button>
    </div>
  `;

  card.addEventListener("click", e => {
    if (e.target.closest(".seg-controls")) return;
    const v = video();
    if (v) {
      v.currentTime = seg.start_time;
      v.play().catch(() => {});
    }
  });

  card.querySelector(".seg-confirm-btn").addEventListener("click", async () => {
    const sel = card.querySelector(".seg-select");
    if (!sel.value) { alert("Select a speaker first."); return; }
    await handleConfirm(seg.segment_id, sel.value, card);
  });

  card.querySelector(".seg-new-btn").addEventListener("click", () => {
    pendingSegmentId = seg.segment_id;
    openPersonDialog();
  });

  return card;
}

async function handleConfirm(segmentId, personId, cardEl) {
  const btn = cardEl?.querySelector(".seg-confirm-btn");
  if (btn) { btn.textContent = "…"; btn.disabled = true; }

  try {
    const assignment = await confirmAssignment(segmentId, personId);

    // Update just this card immediately — don't reload all 1700 segments
    const person = people.find(p => p.person_id === personId);
    if (cardEl && person) {
      const nameEl = cardEl.querySelector(".seg-speaker-name");
      const badgeEl = cardEl.querySelector(".seg-badge");
      if (nameEl) nameEl.textContent = person.canonical_name;
      if (badgeEl) {
        badgeEl.textContent = "✓ verified";
        badgeEl.className = "seg-badge badge-verified";
      }
      cardEl.className = cardEl.className.replace(/conf-\w+/, "conf-verified");
    }
    if (btn) { btn.textContent = "✓"; btn.disabled = false; btn.classList.add("confirmed"); }

    // Update the segment in local state
    const seg = segments.find(s => s.segment_id === segmentId);
    if (seg) {
      seg.assignment = assignment;
    }
    updateStats();

    // Show background reprocessing indicator and poll until it settles
    showReprocessIndicator();

  } catch (err) {
    alert(`Failed to confirm: ${err.message}`);
    if (btn) { btn.textContent = "Confirm"; btn.disabled = false; }
  }
}

// Show indicator, then after a delay reload segments to pick up new predictions
function showReprocessIndicator() {
  const indicator = document.getElementById("reprocess-indicator");
  if (indicator) indicator.classList.add("visible");

  // Clear any existing reprocess poll
  if (reprocessPollTimer) clearTimeout(reprocessPollTimer);

  // Poll a few times to reload segments as background rerun completes
  let attempts = 0;
  const maxAttempts = 6;
  const interval = 5000; // 5s between checks

  function schedulePoll() {
    reprocessPollTimer = setTimeout(async () => {
      await Promise.all([loadPeople(), loadSegments()]);
      attempts++;
      if (attempts < maxAttempts) {
        schedulePoll();
      } else {
        if (indicator) indicator.classList.remove("visible");
        reprocessPollTimer = null;
      }
    }, interval);
  }

  schedulePoll();
}

// ── Stats bar ──────────────────────────────────────────────────────────────────

function updateStats() {
  const total    = segments.length;
  const verified = segments.filter(s => s.assignment?.verified).length;
  const unknown  = segments.filter(s => !s.assignment?.predicted_person_id).length;

  const el = document.getElementById("segment-stats");
  if (total === 0) { el.textContent = "—"; return; }
  el.textContent = `${verified}/${total} verified · ${unknown} unknown`;
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
    if (scrollIntoView) {
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  const seg    = segments[idx];
  const assign = seg.assignment;
  const person = assign?.predicted_person_id
    ? people.find(p => p.person_id === assign.predicted_person_id)
    : null;
  const label  = person?.canonical_name ?? seg.raw_speaker_label ?? "";
  document.getElementById("active-speaker-label").textContent = label;
}

// ── Pipeline polling ──────────────────────────────────────────────────────────

async function checkPipelineAndPoll() {
  const status  = await fetchStatus();
  const hasVideo = await fetchHasVideo();

  if (!hasVideo) {
    document.getElementById("upload-bar").hidden = false;
    return;
  }

  setVideoSource();

  if (!status || status.status === "complete") {
    hidePipelineBanner();
    return;
  }

  // Pipeline still running — show banner with progress
  // BUT also show any segments already in DB (raw transcript)
  showPipelineBanner(status);

  pollTimer = setInterval(async () => {
    const s = await fetchStatus();
    if (!s) return;

    updateBannerProgress(s);

    // Reload segments as they become available (e.g. after transcription commits)
    await loadSegments();

    if (s.status === "complete") {
      clearInterval(pollTimer);
      pollTimer = null;
      hidePipelineBanner();
    }
  }, 4000);
}

async function fetchStatus() {
  try {
    const r = await fetch(`/api/media/${meetingId}/status`);
    return r.ok ? r.json() : null;
  } catch { return null; }
}

async function fetchHasVideo() {
  try {
    const r = await fetch(`/api/media/${meetingId}`);
    if (!r.ok) return false;
    const files = await r.json();
    return files.some(f => f.file_type === "video");
  } catch { return false; }
}

function setVideoSource() {
  const v = video();
  if (!v || v.src) return;
  v.src = `/media/${meetingId}/video.mp4`;
}

function showPipelineBanner(status) {
  const banner = document.getElementById("pipeline-banner");
  banner.hidden = false;
  updateBannerProgress(status);
}

function updateBannerProgress(status) {
  const pct     = status.progress_pct ?? 0;
  const stage   = status.stage || "Processing…";
  const detail  = status.detail || "";
  const indeterminate = pct === 0 && stage !== "";

  document.getElementById("pipeline-banner-text").textContent =
    status.segment_count > 0
      ? `Processing — ${status.segment_count} segments available below`
      : "Processing — transcript will appear when ready";

  const progressEl = document.getElementById("banner-progress");
  if (progressEl) progressEl.style.display = "block";

  const stageEl = document.getElementById("banner-stage");
  const pctEl   = document.getElementById("banner-pct");
  const fillEl  = document.getElementById("banner-bar-fill");
  const detailEl = document.getElementById("banner-detail");

  if (stageEl) stageEl.textContent = stage;
  if (pctEl)   pctEl.textContent   = pct ? `${pct}%` : "";
  if (detailEl) detailEl.textContent = detail;
  if (fillEl) {
    if (indeterminate) {
      fillEl.classList.add("indeterminate");
      fillEl.style.width = "35%";
    } else {
      fillEl.classList.remove("indeterminate");
      fillEl.style.width = `${pct}%`;
    }
  }
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
      btn.textContent = "↻ Queuing…";
      btn.disabled = true;
      try {
        await reprocessAll();
        showReprocessIndicator();
      } finally {
        btn.textContent = "↻ Reprocess";
        btn.disabled = false;
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
    const fakeStatus = { status: "processing", stage: "Starting…", progress_pct: 0, detail: "", segment_count: 0 };
    showPipelineBanner(fakeStatus);
    setVideoSource();

    pollTimer = setInterval(async () => {
      const s = await fetchStatus();
      if (!s) return;
      updateBannerProgress(s);
      await loadSegments();
      if (s.status === "complete") {
        clearInterval(pollTimer);
        pollTimer = null;
        hidePipelineBanner();
      }
    }, 4000);
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
    const hasText = doc.ocr_text && doc.ocr_text.trim().length > 0;
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
    .addEventListener("click", () => dialog.close());

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
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
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
