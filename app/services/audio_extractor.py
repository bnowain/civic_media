"""
Audio extraction service.

Converts any video/audio format to mono 16kHz WAV using ffmpeg.

Preprocessing chain (webcast-optimised):
  1. High-pass filter at 90 Hz  — removes HVAC rumble and low-frequency noise
  2. EBU R128 loudnorm          — normalises loudness across speakers/segments
     Target: I=-16 LUFS, True Peak=-1.5 dBTP, LRA=11 LU
     (single-pass; accurate enough for speech, avoids doubling runtime)
  3. Mono, 16 kHz, PCM s16le    — Whisper's native format

The exact ffmpeg command is logged at INFO level for reproducibility.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Preprocessing constants ───────────────────────────────────────────────────
# Adjust these to tune the audio chain without touching code.

# High-pass cutoff in Hz — removes low-frequency rumble (HVAC, mic handling)
HPF_FREQ_HZ = 90

# EBU R128 loudnorm parameters
LOUDNORM_I   = -16    # integrated loudness target (LUFS)
LOUDNORM_TP  = -1.5   # true peak ceiling (dBTP)
LOUDNORM_LRA = 11     # loudness range target (LU)


def extract_audio(video_path: str, output_path: str) -> float:
    """
    Extract preprocessed mono 16kHz PCM WAV from a video or audio file.

    Applies:
      - High-pass filter (HPF_FREQ_HZ)
      - EBU R128 loudness normalisation (single-pass)
      - Mono downmix at 16 kHz

    Args:
        video_path:  Absolute path to the source video/audio file.
        output_path: Absolute path for the output WAV file.

    Returns:
        Duration of the extracted audio in seconds.

    Raises:
        RuntimeError: If ffmpeg fails.
    """
    duration = _probe_duration(video_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Build the audio filter chain
    af = (
        f"highpass=f={HPF_FREQ_HZ},"
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
    )

    cmd = [
        "ffmpeg",
        "-y",                    # overwrite output
        "-i", video_path,
        "-vn",                   # drop video stream
        "-ac", "1",              # mono
        "-ar", "16000",          # 16 kHz
        "-af", af,               # preprocessing chain
        "-acodec", "pcm_s16le",  # 16-bit PCM
        output_path,
    ]

    logger.info("Audio extraction command: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}):\n"
            f"{result.stderr.decode(errors='replace')}"
        )

    # Use WAV duration as ground truth (more accurate than container metadata)
    wav_dur = get_wav_duration(output_path)
    actual_duration = wav_dur if wav_dur > 0 else duration

    logger.info(
        "Audio extracted: %.1f min  filter=%s",
        actual_duration / 60,
        af,
    )

    return actual_duration


def _probe_duration(video_path: str) -> float:
    """Return video/audio duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        video_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if result.returncode != 0:
        return 0.0

    try:
        info = json.loads(result.stdout)
        return float(info["format"].get("duration", 0.0))
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def get_wav_duration(wav_path: str) -> float:
    """Return duration of a WAV file in seconds."""
    import wave
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate   = wf.getframerate()
            return frames / float(rate)
    except Exception:
        return _probe_duration(wav_path)
