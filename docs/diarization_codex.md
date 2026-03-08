# Diarization Codex

How civic_media identifies who is speaking and why each design decision improves accuracy.

---

## The Problem

A civic meeting recording is a single audio stream with multiple speakers — board members, staff, public commenters — talking one at a time (usually). The goal is to label every sentence in the transcript with the correct person's name. This is called **speaker diarization**, and getting it right is hard because:

- Speakers are not identified in the audio itself. There are no name tags.
- Different speakers can sound similar (same age, same gender, same accent).
- The same speaker sounds different across meetings (different mic, room, energy level).
- Speakers change mid-sentence (a question from one person immediately followed by an answer from another).
- Civic meetings have 10-30+ speakers per session, some only speaking for a few seconds.

Our system solves this through a six-stage pipeline and a human-in-the-loop learning system that gets better with every confirmation.

---

## Pipeline Overview

```
Video/Audio → Extract → Transcribe → Diarize → Align → Embed → Match
                1           2           3         4       5       6
```

Each stage is designed to preserve maximum information for the next stage and is independently resumable (no stage repeats work already done).

---

## Stage 1: Audio Extraction

**What:** FFmpeg converts the source video/audio to mono 16kHz WAV.

**Why this matters for diarization:**

- **High-pass filter at 90 Hz** removes HVAC rumble, air conditioning hum, and low-frequency noise that confuses both the transcriber and the diarizer. These sounds occupy the same frequency range as vocal fundamental frequencies and make speaker boundaries harder to detect.

- **EBU R128 loudness normalization** (-16 LUFS) ensures all speakers are at a consistent volume. Without this, a quiet speaker at a distant microphone produces weaker embeddings, and the diarizer may miss their turns entirely or split them into fragments. Normalization means the diarizer hears every speaker at the same level, regardless of their mic position.

- **Mono downmix** is required because the ML models expect single-channel input. Stereo civic meeting recordings are typically the same signal on both channels anyway.

- **16 kHz sample rate** is the native format for both Whisper (transcription) and pyannote (diarization). Extracting at this rate avoids an implicit resample inside the models, which could introduce artifacts at speaker boundaries.

---

## Stage 2: Transcription (faster-whisper)

**What:** Whisper large-v3 converts audio to text with word-level timestamps.

**Key settings and why:**

- **`beam_size=10`** — Maximum practical beam width for large-v3. More beams means the decoder explores more hypotheses, producing more accurate text and more precise timestamps. Accurate word timestamps are critical because they feed directly into the alignment stage.

- **`language="en"`** — Forced English. Auto-detect can flip to another language on noisy segments (applause, crosstalk), which produces garbage text and wrong timestamps. Forcing English eliminates this non-determinism.

- **`temperature=(0, 0.2, 0.4, 0.6, 0.8, 1.0)`** — Starts with fully deterministic decoding (temperature=0). If the output has high compression ratio (repetition) or low log-probability, Whisper automatically retries with progressively higher temperatures. This prevents the decoder from getting stuck in repetition loops on silence or noise sections, while keeping output deterministic when the audio is clean.

- **`word_timestamps=True`** — This is the single most important setting for diarization accuracy. Without word timestamps, the aligner can only assign whole Whisper segments (5-30 seconds) to one speaker. With word timestamps, we can split a segment at the exact word where the speaker changes. A Whisper segment that contains both a question and its answer gets correctly split between two speakers instead of being assigned entirely to one.

- **`vad_filter=True`** with explicit parameters — Voice Activity Detection pre-filters silence before Whisper processes it. This prevents Whisper from hallucinating text during silent gaps (a common source of phantom segments). The parameters are pinned explicitly so the same audio always produces the same segment boundaries.

- **Hallucination filtering** — Four filters remove garbage before it reaches diarization:
  - `compression_ratio > 2.4` → Whisper is looping on repeated phrases (zlib compression ratio measures text repetitiveness)
  - `no_speech_prob > 0.9` → Whisper is transcribing silence
  - `duration < 0.1s` → Zero-duration phantom segments (Whisper emitted text without advancing the audio position)
  - `chars/sec > 25 with 30+ chars` → Ghost segments: full sentences crammed into impossibly short durations (normal fast speech ~20 c/s). Added 2026-03-08 after finding 5,020 ghosts in existing data.

- **Vocab hints** — Domain-specific terms (names of board members, local agencies, places) are fed to Whisper via `initial_prompt`. This dramatically improves proper noun accuracy — "Supervisor Crye" instead of "supervisor cry" — which matters because the transcript text is what the human reviewer reads when deciding who is speaking.

---

## Stage 3: Diarization (pyannote.audio 3.1)

**What:** pyannote's speaker diarization pipeline labels every time interval with an anonymous speaker tag (SPEAKER_00, SPEAKER_01, etc.).

**How it works internally:**

1. **Voice Activity Detection** — Finds speech regions in the audio
2. **Speaker Embedding Extraction** — Extracts a vector representation of the speaker's voice for each speech region
3. **Clustering** — Groups speech regions by speaker similarity (agglomerative clustering)
4. **Overlapped Speech Detection** — Identifies regions where two speakers talk simultaneously

**Why pyannote 3.1:**

- State-of-the-art on standard benchmarks (AMI, DIHARD, VoxConverse)
- Handles overlapping speech — critical for civic meetings where speakers talk over each other during heated discussions
- GPU-accelerated — runs on CUDA for 10-20x speedup vs CPU
- The output is cached to `diarization.json` so re-running the pipeline (e.g., after fixing a Whisper issue) doesn't repeat the most expensive stage

**What diarization does NOT do:** It assigns anonymous labels (SPEAKER_00), not names. The labels are consistent within a single meeting but meaningless across meetings. Turning SPEAKER_00 into "Kevin Crye" is the job of the voiceprint system (stages 5-6).

---

## Stage 4: Alignment

**What:** Merges Whisper's transcript segments with pyannote's speaker turns by time overlap.

**This is where the two streams meet, and the details matter:**

### Word-Level Alignment (preferred)

When word timestamps are available (the normal case), each individual word's time span is compared against the diarization speaker turns. The word is assigned to the speaker with maximum time overlap. Then consecutive same-speaker words are regrouped into segments.

**Why this is critical:** Whisper often produces segments that span a speaker change. For example, one Whisper segment might contain "...any questions on this item? Yes, I'd like to..." where the first part is the chair and the second is a board member. Word-level alignment splits this at the exact word where the speaker changes.

### Segment-Level Alignment (fallback)

When word timestamps aren't available (legacy data or cache-only resume), whole Whisper segments are assigned to the speaker with the most time overlap. This is less accurate at speaker boundaries but better than no alignment.

### Merging Adjacent Fragments

After speaker assignment, the aligner merges adjacent segments from the same speaker:

- **Gap ≤ 1.5 seconds** — Brief pauses within a speaker's turn are merged. Without this, a speaker who pauses to think would have their turn split into many tiny fragments, each requiring separate review.
- **Short segments (< 3s)** are absorbed into adjacent same-speaker blocks — Prevents single-word fragments that are too short for meaningful speaker embeddings.
- **Cap at 5 sentences** — Prevents creating enormous merged blocks. Long blocks are hard to review and produce worse embeddings (the voice characteristics of a 2-minute stretch are diluted compared to a focused 10-second clip).

### Runt Absorption

A second pass catches very short fragments that survived initial merging. If a sub-3-second segment has the same speaker as its predecessor, it gets merged in. This cleans up edge cases where the diarizer briefly flickered to a different speaker label and back.

---

## Stage 5: Speaker Embedding Extraction (SpeechBrain ECAPA-TDNN)

**What:** For every transcript segment, extract a 192-dimensional vector that represents the speaker's voice characteristics.

**Why ECAPA-TDNN:**

- Specifically designed for speaker verification (is this the same person?)
- Trained on VoxCeleb — diverse speakers across languages, accents, recording conditions
- Produces compact 192-dimensional embeddings that are fast to compare
- Well-suited to short audio clips (2-10 seconds) — exactly the length of transcript segments

**Key design decisions:**

- **Batch processing (32 segments per GPU pass)** — Instead of running 1700 sequential single-item passes for a long meeting, segments are grouped into batches. Similar-length segments are grouped together to minimize padding waste. This keeps the GPU fully utilized and reduces processing time by ~20x.

- **Audio capped at 10 seconds** — ECAPA-TDNN doesn't benefit from audio longer than ~10 seconds. Beyond that, the embedding becomes an average that dilutes distinctive vocal characteristics. Capping at 10s ensures the embedding captures the most distinctive part of the speaker's voice.

- **Minimum duration 0.5 seconds** — Segments shorter than this don't contain enough speech for a reliable embedding. These are skipped rather than producing noisy embeddings that would degrade matching.

- **Audio cached in memory** — The full waveform is loaded once and kept in RAM for the entire embedding loop. Without this, every segment extraction would re-read the entire WAV file from disk (1700+ times for a long meeting). The cache is cleared after the loop completes to free memory.

- **Embeddings stored as serialized numpy arrays in SQLite** — Not a vector database, not a separate service. The embeddings live alongside the transcript data they belong to. This keeps the system simple and self-contained. Cosine similarity on 192-dimensional vectors is fast enough that brute-force comparison against all voiceprints takes milliseconds.

---

## Stage 6: Voiceprint Matching

**What:** Compare each segment's embedding against the library of known person voiceprints to predict who is speaking.

### Top-K Similarity Scoring

Instead of comparing against a single centroid (average) per person, we compare against all individual voiceprints and take the **mean of the top-3 best matches** (Top-K, K=3).

**Why Top-K instead of centroid:**

- A single centroid averages out distinctive vocal characteristics, especially if a person has voiceprints from different microphones or energy levels. The centroid drifts toward a bland middle.
- Top-K captures the best evidence from the most relevant voiceprints. If a person has 20 voiceprints and 3 of them are from a similar recording setup as the current segment, those 3 will naturally score highest and drive the match.
- Top-K is robust to a few bad voiceprints (confirmed on the wrong person). They won't be in the top 3 unless the person genuinely sounds like that.

### Confidence Thresholds

| Tier | Score | Behavior |
|------|-------|----------|
| **High** | ≥ 0.92 | Very likely correct. Displayed as "high confidence" in the UI |
| **Medium** | ≥ 0.75 | Probable match. Auto-assigned, displayed as "medium confidence" |
| **Below threshold** | < 0.75 | Not assigned. Segment shows as "unknown" for manual review |

**Why 0.75 and not lower:** Below 0.75, false positive rate increases sharply. A wrong auto-assignment is worse than no assignment — the user has to notice it's wrong AND correct it, rather than just assigning from blank. The 0.75 threshold was tuned to minimize false positives while still auto-assigning the majority of segments for known speakers.

**Why 0.92 for "high":** At this level, misidentification is extremely rare. The UI displays these segments with strong visual confidence so the reviewer can quickly skip past them and focus attention on medium and unknown segments.

### Coherence Gate

Before matching begins, voiceprints are filtered through a coherence gate:

1. For each person, compute the centroid (mean) of all their voiceprints
2. Exclude any voiceprint with cosine similarity to the centroid below 0.6
3. Safety: never exclude ALL voiceprints for a person — always keep at least the one closest to the centroid

**Why this exists:** If a voiceprint was accidentally confirmed on the wrong person, it would be an outlier compared to that person's other voiceprints. The coherence gate detects and excludes these outliers from matching, preventing them from pulling the wrong segments toward the wrong person. Critically, the bad voiceprint is excluded from matching but **not deleted** — if the user later re-confirms it correctly, it naturally becomes coherent with its new person's pool.

---

## The Learning Loop

This is the core of the system. It turns human review into permanent learning.

### When a user confirms a speaker assignment:

1. The segment's embedding is stored as a new **Voiceprint** row for that person (purely additive — nothing is deleted)
2. The **SegmentAssignment** is marked `verified=True` (never auto-touched again)
3. A background task re-runs voiceprint matching on **all unverified segments** across the meeting

### Why this is powerful:

- **One confirmation improves many segments.** If a user confirms that segment #47 is "Kevin Crye", the system now has a new voiceprint for Kevin. The background re-run may auto-match 30 other segments where Kevin was speaking, saving the reviewer from manually confirming each one.

- **Learning compounds across meetings.** Voiceprints are global — they belong to a person, not a meeting. After confirming Kevin Crye in 3 meetings, the system has voiceprints from 3 different recording setups (different mics, rooms, days). This makes matching more robust for future meetings because the voiceprint pool now represents the natural variation in Kevin's voice.

- **Purely additive, never destructive.** Old voiceprints are never deleted by automatic processes. Centroids are recomputed fresh every time from all retained voiceprints. This means the system can only get better, never forget what it learned (short of a manual deletion).

- **Verified segments are sacrosanct.** Once a human says "this is Kevin Crye", no automatic process will ever overwrite that. The system will never change a verified assignment even if new voiceprints suggest otherwise. Human judgment always wins.

- **Tagged vs. Confirmed.** Users can "tag" a segment (assign without training) or "confirm" it (assign AND create a voiceprint). Tagging is useful when the speaker is known but the audio quality is poor — you don't want a noisy embedding polluting the voiceprint pool. Both tagged and confirmed assignments are protected from auto-reassignment.

### The flywheel effect:

```
Confirm speaker → New voiceprint added → Background re-match →
More segments auto-identified → Fewer unknowns to review →
Next confirmation is faster → More voiceprints → Even better matching
```

After processing 5-10 meetings for the same governing body, the system typically auto-identifies 80-90% of segments at medium or high confidence, leaving only new/rare speakers and ambiguous moments for manual review.

---

## Why This Architecture Works for Civic Meetings

Civic meetings have properties that make this approach especially effective:

1. **Repeat speakers.** The same board members, staff, and frequent public commenters appear meeting after meeting. Every meeting processed adds voiceprints that improve identification in future meetings.

2. **Structured turn-taking.** Unlike casual conversation, civic meetings follow parliamentary procedure — speakers are recognized, take the podium, and yield. This creates clean speaker boundaries that diarization handles well.

3. **Consistent recording setup.** Most civic bodies use the same room, same microphones, same recording system. This means voiceprints are more stable across meetings than they would be for, say, phone calls or field recordings.

4. **Known speaker universe.** The set of possible speakers is finite and mostly known (board members, department heads). The system doesn't need to identify strangers — it needs to match against a library of ~50-200 known individuals that grows over time.

5. **Human reviewers have context.** A reviewer watching the video knows who is speaking from visual cues. This makes confirmation fast and accurate, which means high-quality voiceprints, which means better automatic matching.

---

## Configuration Reference

| Setting | Value | Purpose |
|---------|-------|---------|
| `WHISPER_MODEL` | large-v3 | Most accurate Whisper model |
| `BEAM_SIZE` | 10 | Maximum practical beam search width |
| `TEMPERATURE` | (0, 0.2, 0.4, 0.6, 0.8, 1.0) | Retry with randomness if deterministic decode fails |
| `PYANNOTE_MODEL` | speaker-diarization-3.1 | State-of-the-art diarization |
| `EMBEDDING_MODEL` | spkrec-ecapa-voxceleb | Speaker verification embeddings |
| `SIMILARITY_HIGH` | 0.92 | High-confidence threshold |
| `SIMILARITY_MEDIUM` | 0.75 | Auto-assignment threshold |
| `VOICEPRINT_COHERENCE_THRESHOLD` | 0.6 | Outlier exclusion gate |
| `TOP_K` | 3 | Voiceprints to average per person |
| `MAX_EMBED_AUDIO_SEC` | 10.0 | Embedding audio cap (seconds) |
| `MIN_EMBED_DURATION` | 0.5 | Minimum segment for embedding |
| `MAX_MERGE_GAP` | 1.5s | Adjacent same-speaker merge threshold |
| `MIN_SEGMENT_DURATION` | 3.0s | Runt absorption threshold |
| `MAX_MERGE_SENTENCES` | 5 | Sentence cap per merged segment |
| `HALLUCINATION_COMPRESSION_RATIO` | 2.4 | Whisper repetition filter |
| `MIN_SEGMENT_DURATION` (transcriber) | 0.1s | Zero-duration artifact filter |
