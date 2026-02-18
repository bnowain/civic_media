# Civic Meeting Media Processing Tool
## Phase 1 — Local Transcription, Diarization & Speaker Refinement

A focused, local-first tool for processing government meeting recordings.
Load a video. Transcribe it. Diarize speakers. Review and correct assignments.
The system learns from every correction.

---

## What It Does

1. Ingests meeting videos (MP4, MKV, MOV, AVI, WebM)
2. Extracts mono 16kHz audio via ffmpeg
3. Transcribes speech with timestamps (faster-whisper)
4. Diarizes speakers (pyannote.audio)
5. Aligns transcript + diarization segments
6. Extracts speaker embeddings (SpeechBrain ECAPA-TDNN)
7. Matches segments against a voiceprint library (cosine similarity)
8. Stores everything in SQLite
9. Provides a side-by-side video + transcript review interface
10. Learns incrementally from every human correction
11. Accepts agenda/minutes PDFs (with OCR fallback via Tesseract)

**It does nothing else.**

---

## What It Does Not Do

- Summaries
- Search
- Analytics
- Statistics
- Participation metrics
- Cloud sync
- Multi-user support
- Face recognition
- Anything beyond ingestion and speaker refinement

---

## Directory Structure

```
civic_media/
├── app/
│   ├── main.py              # FastAPI app
│   ├── config.py            # Paths, thresholds, model names
│   ├── database.py          # SQLAlchemy engine + session
│   ├── models.py            # ORM table definitions
│   ├── schemas.py           # Pydantic request/response models
│   ├── tasks.py             # Celery task definitions
│   ├── worker.py            # Celery app instance
│   ├── routers/             # API route handlers
│   │   ├── meetings.py
│   │   ├── media.py
│   │   ├── documents.py
│   │   ├── segments.py
│   │   ├── people.py
│   │   └── assignments.py   # Voiceprint learning loop
│   ├── services/            # Business logic
│   │   ├── audio_extractor.py
│   │   ├── transcriber.py
│   │   ├── diarizer.py
│   │   ├── aligner.py
│   │   ├── embedder.py
│   │   ├── voiceprint.py    # Cosine similarity + centroid computation
│   │   ├── pdf_ingestor.py
│   │   └── pipeline.py      # Orchestrates full video pipeline
│   └── static/
│       ├── index.html       # Meeting list
│       ├── review.html      # Side-by-side review interface
│       ├── index.js
│       ├── review.js
│       └── style.css
├── database/
│   └── civic_media.db       # SQLite database
├── media/
│   └── {meeting_id}/
│       ├── video.mp4
│       └── audio.wav
├── documents/
│   └── {meeting_id}/
│       └── agenda.pdf
├── ocr_text/
│   └── {meeting_id}/
│       └── agenda.txt
├── requirements.txt
├── docker-compose.yml        # Redis only
└── run.sh
```

---

## Prerequisites

### System packages

```bash
# Ubuntu/Debian
sudo apt install ffmpeg tesseract-ocr poppler-utils

# macOS
brew install ffmpeg tesseract poppler
```

### Python

Python 3.11 or 3.12 required.

```bash
pip install -r requirements.txt
```

For CUDA (RTX 5090 / any NVIDIA GPU):
```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### Redis

```bash
docker compose up -d redis
# or: sudo apt install redis-server && sudo systemctl start redis
```

### HuggingFace Token (required for pyannote)

1. Create an account at https://huggingface.co
2. Accept the model licence at https://hf.co/pyannote/speaker-diarization-3.1
3. Create a token at https://hf.co/settings/tokens
4. Export it:
   ```bash
   export HF_TOKEN=hf_your_token_here
   ```

---

## Running

```bash
export HF_TOKEN=hf_your_token_here
./run.sh
```

Open http://localhost:8000 in your browser.

---

## Configuration

Edit `app/config.py` or set environment variables:

| Variable          | Default      | Description                             |
|-------------------|--------------|-----------------------------------------|
| `HF_TOKEN`        | (required)   | HuggingFace token for pyannote          |
| `WHISPER_MODEL`   | `large-v3`   | Whisper model size                      |
| `WHISPER_DEVICE`  | `cuda`       | `cuda` or `cpu`                         |
| `WHISPER_COMPUTE` | `float16`    | `float16`, `int8`, or `float32`         |
| `CELERY_BROKER`   | `redis://localhost:6379/0` | Celery broker URL      |

---

## Voiceprint Learning Loop

Every time you confirm a speaker assignment in the review interface:

1. The segment's embedding is added as a new voiceprint for that person (never overwritten)
2. A fresh centroid is computed from ALL embeddings for that person
3. ALL unverified segments in the meeting are re-evaluated against updated centroids
4. New predictions appear immediately in the interface

Confidence thresholds:
- **High** (≥ 0.92): Strong match, shown with blue left border
- **Medium** (0.75–0.91): Plausible match, shown with amber left border
- **Unknown** (< 0.75): No match, shown with gray left border
- **Verified**: Human-confirmed, shown with green left border

Verified assignments are never touched by automatic matching.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/meetings/` | List meetings |
| POST | `/api/meetings/` | Create meeting |
| GET | `/api/meetings/{id}` | Get meeting |
| POST | `/api/media/{id}/upload` | Upload video → triggers pipeline |
| GET | `/api/media/{id}/status` | Pipeline status |
| POST | `/api/documents/{id}/upload` | Upload PDF → triggers OCR |
| GET | `/api/segments/{id}` | List transcript segments |
| GET | `/api/people/` | List all known speakers |
| POST | `/api/people/` | Create new speaker |
| **POST** | **`/api/assignments/{seg_id}/confirm`** | **Confirm speaker (learning loop)** |
| POST | `/api/assignments/reprocess/{meeting_id}` | Re-run all predictions |
| GET | `/api/docs` | Interactive API documentation |

---

## Database Schema

Six tables only, as specified:

- `meetings` — meeting metadata
- `media_files` — video and audio file paths
- `documents` — PDF paths and extracted text
- `transcript_segments` — timestamped text + raw speaker label + embedding blob
- `people` — canonical speaker names
- `voiceprints` — per-person embedding blobs (additive, never deleted)
- `segment_assignments` — predicted speaker + similarity score + verified flag

---

## Scope

This tool will not gain additional features. Phase 1 is complete when:

- A meeting video can be uploaded and processed end-to-end
- The review interface shows a synchronised side-by-side layout
- Speaker assignments can be corrected and confirmed
- Voiceprint learning demonstrably improves predictions over time
- PDFs can be uploaded and text extracted
- All data persists in SQLite on disk

Nothing beyond this scope.
# civic_media
