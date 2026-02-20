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
Progress is reported via an optional callback during extraction.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── Preprocessing constants ───────────────────────────────────────────────────
# Adjust these to tune the audio chain without touching code.

# High-pass cutoff in Hz — removes low-frequency rumble (HVAC, mic handling)
HPF_FREQ_HZ = 90

# EBU R128 loudnorm parameters
LOUDNORM_I   = -16    # integrated loudness target (LUFS)
LOUDNORM_TP  = -1.5   # true peak ceiling (dBTP)
LOUDNORM_LRA = 11     # loudness range target (LU)

# Regex to extract the time= field from ffmpeg's progress output
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def extract_audio(
    video_path: str,
    output_path: str,
    on_progress: Callable[[float], None] | None = None,
) -> float:
    """
    Extract preprocessed mono 16kHz PCM WAV from a video or audio file.

    Applies:
      - High-pass filter (HPF_FREQ_HZ)
      - EBU R128 loudness normalisation (single-pass)
      - Mono downmix at 16 kHz

    Args:
        video_path:  Absolute path to the source video/audio file.
        output_path: Absolute path for the output WAV file.
        on_progress: Optional callback receiving a float 0.0–1.0 representing
                     extraction progress (based on ffmpeg's time output vs
                     total duration).

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

    if on_progress and duration > 0:
        returncode, stderr_text = _run_with_progress(cmd, duration, on_progress)
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        returncode = result.returncode
        stderr_text = result.stderr.decode(errors="replace")

    if returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {returncode}):\n{stderr_text}"
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


def _run_with_progress(
    cmd: list[str],
    total_duration: float,
    on_progress: Callable[[float], None],
) -> tuple[int, str]:
    """
    Run ffmpeg via Popen, parse stderr for time= progress, and call
    on_progress(fraction) periodically.

    Returns (returncode, stderr_text).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stderr_chunks = []
    last_pct = -1

    # ffmpeg writes progress to stderr. Read it character-by-character
    # and parse line-by-line. ffmpeg uses \r for progress lines.
    buf = ""
    while True:
        chunk = proc.stderr.read(256)
        if not chunk:
            break
        text = chunk.decode(errors="replace")
        stderr_chunks.append(text)
        buf += text

        # Split on \r or \n to get progress lines
        while "\r" in buf or "\n" in buf:
            line, _, buf = re.split(r"[\r\n]", buf, maxsplit=1)
            m = _TIME_RE.search(line)
            if m and total_duration > 0:
                h, mn, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
                elapsed = h * 3600 + mn * 60 + s
                pct = int(min(elapsed / total_duration, 1.0) * 100)
                if pct != last_pct:
                    last_pct = pct
                    on_progress(elapsed / total_duration)

    proc.wait()
    return proc.returncode, "".join(stderr_chunks)


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
