"""
Audio extraction service.
Converts any video format to mono 16kHz WAV using ffmpeg.
"""

import subprocess
from pathlib import Path


def extract_audio(video_path: str, output_path: str) -> float:
    """
    Extract mono 16kHz PCM WAV from a video file.

    Args:
        video_path:  Absolute path to the source video.
        output_path: Absolute path for the output WAV file.

    Returns:
        Duration of the audio in seconds.

    Raises:
        RuntimeError: If ffmpeg fails or the file cannot be probed.
    """
    # Probe duration first
    duration = _probe_duration(video_path)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",                    # overwrite output
        "-i", video_path,
        "-vn",                   # drop video stream
        "-ac", "1",              # mono
        "-ar", "16000",          # 16 kHz
        "-acodec", "pcm_s16le",  # 16-bit PCM
        output_path,
    ]

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

    return duration


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
        # Fall back to 0 — duration will be updated later from the WAV
        return 0.0

    import json
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
