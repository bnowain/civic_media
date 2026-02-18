# civic_media

A local transcription and speaker diarization tool for civic meeting recordings. Built for accountability journalism — processes city council, county supervisor, and other public meeting videos into searchable, speaker-attributed transcripts.

## What it does

1. **Upload** a video recording of a public meeting
2. **Transcribe** speech to text using Whisper large-v3 (via faster-whisper)
3. **Diarize** — identify and separate speakers using pyannote.audio
4. **Attribute** — match speaker turns to known individuals using voice embeddings (SpeechBrain ECAPA-TDNN)
5. **Review** — side-by-side video + transcript interface with click-to-seek and speaker confirmation

Each confirmed speaker identification feeds back into a voiceprint library that automatically matches future segments, improving over time with use.

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI + SQLAlchemy |
| Task queue | Celery (solo pool, single worker) |
| Database | SQLite |
| Transcription | faster-whisper (large-v3) |
| Diarization | pyannote.audio 3.3.2 |
| Speaker embeddings | SpeechBrain ECAPA-TDNN |
| Frontend | Vanilla JS + IBM Plex fonts |

## Hardware requirements

- **GPU**: NVIDIA RTX 5090 (or other CUDA 12.8 capable GPU)
- **RAM**: 32GB+ recommended (64GB+ for long meetings)
- **Disk**: ~5GB per 3-hour meeting (video + WAV + database)

> **Note**: This project runs PyTorch nightly (`2.12.0.dev+cu128`) required for RTX 5090 CUDA 12.8 support. Several library patches are required as a result — see [INSTALL.md](INSTALL.md).

## Project structure

```
civic_media/
├── app/
│   ├── routers/
│   │   ├── assignments.py   # Speaker confirmation + voiceprint learning
│   │   ├── media.py         # Video upload + pipeline status/progress
│   │   ├── meetings.py      # Meeting CRUD
│   │   └── ...
│   ├── services/
│   │   ├── pipeline.py      # Orchestrates all processing steps
│   │   ├── transcriber.py   # faster-whisper wrapper
│   │   ├── diarizer.py      # pyannote.audio wrapper
│   │   ├── embedder.py      # SpeechBrain ECAPA embedding
│   │   ├── aligner.py       # Merge transcript + diarization
│   │   ├── voiceprint.py    # Cosine similarity matching engine
│   │   └── audio_extractor.py
│   ├── static/
│   │   ├── index.html/js    # Meeting list with live progress bars
│   │   └── review.html/js   # Video + transcript review interface
│   ├── models.py
│   ├── schemas.py
│   ├── config.py
│   ├── database.py
│   ├── tasks.py             # Celery task definitions
│   └── worker.py
├── media/                   # Created at runtime — video/audio files
├── test_diarizer.py          # Standalone diarization test script
├── INSTALL.md
└── README.md
```

## Pipeline details

Processing is fully resumable. If the worker crashes mid-run, restarting picks up from the last completed checkpoint:

| Checkpoint | What's saved |
|---|---|
| After audio extraction | `MediaFile` record + `audio.wav` on disk |
| After transcription | Raw `TranscriptSegment` rows (no speaker labels yet) |
| After diarization + alignment + embedding | Completed segments with speaker labels and voice embeddings |

Progress is written to `media/{meeting_id}/progress.json` and polled by the UI every 4 seconds.

## Speaker learning

The voiceprint system is purely additive — nothing is ever deleted or overwritten:

- Each human confirmation adds the segment's embedding as a new `Voiceprint` row
- Person centroids are computed fresh each time (mean of all embeddings)
- All unverified segments are re-evaluated in the background after each confirmation
- Confidence tiers: **high** ≥ 92%, **medium** ≥ 75%, **unknown** below that
- Verified assignments are never touched by automatic matching

## Development

See [INSTALL.md](INSTALL.md) for full setup including required library patches.

Quick start after setup:

```powershell
# Terminal 1 — API server
cd E:\0-Automated-Apps\civic_media
venv\Scripts\activate
uvicorn app.main:app --reload

# Terminal 2 — Celery worker
cd E:\0-Automated-Apps\civic_media
venv\Scripts\activate
celery -A app.worker worker --loglevel=info --concurrency=1 --pool=solo
```

Then open http://localhost:8000

## Known limitations

- Single-worker Celery setup — one video processes at a time
- PyTorch nightly required for RTX 5090; library patches must be reapplied after venv rebuild
- Diarization is slow (~10-20 min per hour of audio on RTX 5090)
- Embedding loop (~1700 segments for a 3-hour meeting) is the largest memory consumer
