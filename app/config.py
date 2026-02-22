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

# Minimum segment duration (seconds) for embedding extraction
MIN_EMBED_DURATION = 0.5

# Ensure all directories exist at import time
for _d in [DATABASE_PATH.parent, MEDIA_DIR, DOCUMENTS_DIR, OCR_TEXT_DIR, TV_NEWS_DIR, COMSKIP_INI_DIR]:
    _d.mkdir(parents=True, exist_ok=True)
