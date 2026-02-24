"""
Central configuration for Civic Meeting Media Processing Tool.
All paths, thresholds, and model identifiers live here.
"""

import os
from pathlib import Path

BASE_DIR       = Path(__file__).parent.parent
DATABASE_PATH  = BASE_DIR / "database" / "civic_media.db"
MEDIA_DIR      = BASE_DIR / "media"
DOCUMENTS_DIR  = BASE_DIR / "documents"
OCR_TEXT_DIR   = BASE_DIR / "ocr_text"
TV_NEWS_DIR    = BASE_DIR / "tv_news"
CLIPS_DIR      = BASE_DIR / "media" / "clips"
COMSKIP_INI_DIR = BASE_DIR / "config" / "comskip"

# Atlas API (for LLM-powered story segmentation and tagging)
ATLAS_API_URL  = os.environ.get("ATLAS_API_URL", "http://localhost:8888/api")

DATABASE_URL   = f"sqlite:///{DATABASE_PATH}"
CELERY_BROKER  = os.environ.get("CELERY_BROKER", "redis://localhost:6379/0")
CELERY_BACKEND = os.environ.get("CELERY_BACKEND", "redis://localhost:6379/1")

# Voiceprint similarity thresholds
SIMILARITY_HIGH   = 0.92
SIMILARITY_MEDIUM = 0.75

# Coherence gate: exclude voiceprints below this cosine similarity
# to their person's centroid. Prevents bad confirmations from polluting
# matching without deleting the voiceprint (it may be from a different venue).
VOICEPRINT_COHERENCE_THRESHOLD = 0.6

# Maximum audio (seconds) fed to ECAPA-TDNN per segment.
# Longer audio doesn't improve embedding quality and causes CUDA
# 32-bit index overflow on large batches.
MAX_EMBED_AUDIO_SEC = 10.0

# Multi-clip voiceprints: for long confirmed segments, extract additional
# embeddings from non-overlapping windows beyond the first MAX_EMBED_AUDIO_SEC.
MULTI_CLIP_DURATION    = 8.0    # seconds per extra clip
MULTI_CLIP_MIN_SEGMENT = 20.0   # only extract multi-clips from segments >= this long

# Whisper: tiny | base | small | medium | large-v3
# large-v3 recommended for RTX 5090
WHISPER_MODEL    = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE   = os.environ.get("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE  = os.environ.get("WHISPER_COMPUTE", "float16")

# pyannote — requires HF_TOKEN env var
PYANNOTE_MODEL   = "pyannote/speaker-diarization-3.1"

# SpeechBrain ECAPA-TDNN speaker embeddings
EMBEDDING_MODEL  = "speechbrain/spkrec-ecapa-voxceleb"
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE", "cuda")

# Venue familiarity boost: added to cosine similarity when a voiceprint
# was recorded in the same venue as the segment being matched.
VENUE_FAMILIARITY_BOOST = 0.05

# Minimum segment duration (seconds) for embedding extraction
MIN_EMBED_DURATION = 0.5

# Center extraction margins (seconds) trimmed from segment boundaries before
# embedding extraction. Pyannote detects speaker changes ~200-500ms late,
# so both edges may contain adjacent speakers' voices.
EMBED_START_MARGIN = 0.5   # trim from segment start (incoming speaker bleed)
EMBED_END_MARGIN   = 2.0   # trim from segment end (outgoing speaker bleed)

# Maximum overlap ratio for voiceprint creation. Segments with more than this
# fraction of their duration overlapping with other diarization speakers are
# too contaminated for reliable voiceprints.
MAX_OVERLAP_FOR_VOICEPRINT = 0.15

# Clip export constraints
CLIP_MIN_DURATION   = 0.5     # seconds
CLIP_MAX_DURATION   = 3600.0  # seconds
CLIP_CLEANUP_HOURS  = 24      # hours before auto-cleanup of exported files

# Progress staleness detection
PROGRESS_STALE_SECONDS = 600    # 10 min: status endpoint reports "error" if progress.json is older
ORPHAN_RECOVERY_SECONDS = 120   # 2 min: worker startup re-queues orphaned in-progress tasks

# Ensure all directories exist at import time
for _d in [DATABASE_PATH.parent, MEDIA_DIR, DOCUMENTS_DIR, OCR_TEXT_DIR, TV_NEWS_DIR, CLIPS_DIR, COMSKIP_INI_DIR]:
    _d.mkdir(parents=True, exist_ok=True)
