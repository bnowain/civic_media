"""Transcription-as-a-service endpoint for external callers (e.g. Shasta PRA)."""

import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from app.services.audio_extractor import extract_audio
from app.services.transcriber import transcribe

router = APIRouter(prefix="/api/transcribe", tags=["transcribe"])


@router.post("")
async def transcribe_file(file: UploadFile):
    """Accept an audio/video file upload, transcribe it, return text + segments.

    Returns JSON with: text, segments, duration_seconds, processing_seconds.
    Segments include start, end, text, avg_logprob, no_speech_prob (no word-level).
    """
    if not file.filename:
        raise HTTPException(400, "No file provided")

    t0 = time.time()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Save upload to temp file
        upload_path = tmp / file.filename
        with open(upload_path, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):
                f.write(chunk)

        # Extract audio (ffmpeg → 16kHz mono WAV)
        wav_path = tmp / "audio.wav"
        try:
            duration = extract_audio(str(upload_path), str(wav_path))
        except Exception as e:
            raise HTTPException(422, f"Audio extraction failed: {e}")

        # Transcribe (faster-whisper, no diarization)
        try:
            segments_raw = transcribe(str(wav_path), audio_duration=duration)
        except Exception as e:
            raise HTTPException(500, f"Transcription failed: {e}")

    # Strip word-level data, keep segment-level info
    segments = []
    for seg in segments_raw:
        segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"],
            "avg_logprob": seg.get("avg_logprob", 0.0),
            "no_speech_prob": seg.get("no_speech_prob", 0.0),
        })

    full_text = " ".join(s["text"].strip() for s in segments if s["text"].strip())

    return {
        "text": full_text,
        "segments": segments,
        "duration_seconds": duration,
        "processing_seconds": round(time.time() - t0, 2),
    }
