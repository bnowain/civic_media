"""
Commercial stripping service.

Uses Comskip to detect commercials, then FFmpeg to re-encode without them.
Falls back to CPU libx265 if NVENC is unavailable.

Flow:
  1. Run Comskip on the source video → produces EDL file
  2. Parse EDL → list of (start, end) commercial break tuples
  3. Build FFmpeg complex filter to skip commercial segments
  4. Re-encode to cleaned output file
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from app.config import COMSKIP_INI_DIR

logger = logging.getLogger(__name__)


def _find_comskip_ini(station: str) -> str:
    """Return station-specific Comskip INI if available, else default."""
    station_ini = COMSKIP_INI_DIR / f"{station.lower()}.ini"
    if station_ini.exists():
        return str(station_ini)
    default_ini = COMSKIP_INI_DIR / "default.ini"
    if default_ini.exists():
        return str(default_ini)
    return ""


def _parse_edl(edl_path: Path) -> list[tuple[float, float]]:
    """
    Parse an EDL file into a list of (start_sec, end_sec) commercial breaks.
    EDL format: start_time  end_time  action_type
    Action type 0 = cut, 1 = mute, 2 = scene marker, 3 = commercial break.
    We treat types 0 and 3 as commercials to strip.
    """
    breaks = []
    if not edl_path.exists():
        return breaks
    for line in edl_path.read_text().strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            try:
                start = float(parts[0])
                end = float(parts[1])
                action = int(parts[2])
                if action in (0, 3):
                    breaks.append((start, end))
            except ValueError:
                continue
    return breaks


def _build_keep_segments(
    breaks: list[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """Invert commercial breaks into content keep-segments."""
    if not breaks:
        return [(0, duration)]

    segments = []
    prev_end = 0.0
    for start, end in sorted(breaks):
        if start > prev_end:
            segments.append((prev_end, start))
        prev_end = max(prev_end, end)
    if prev_end < duration:
        segments.append((prev_end, duration))
    return segments


def strip_commercials(
    source_path: str,
    output_path: str,
    station: str = "",
    on_progress: callable = None,
) -> tuple[str, list[tuple[float, float]]]:
    """
    Strip commercials from a video file.

    Args:
        source_path: Path to the source video.
        output_path: Path for the cleaned output video.
        station: Station name for config lookup.
        on_progress: Optional callback(fraction: float).

    Returns:
        (output_path, commercial_breaks) tuple.
    """
    source = Path(source_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Run Comskip
    edl_path = source.with_suffix(".edl")
    ini_path = _find_comskip_ini(station)

    comskip_cmd = ["comskip", "--output", str(source.parent)]
    if ini_path:
        comskip_cmd.extend(["--ini", ini_path])
    comskip_cmd.append(str(source))

    logger.info("Running Comskip: %s", " ".join(comskip_cmd))
    try:
        result = subprocess.run(
            comskip_cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max
        )
        if result.returncode not in (0, 1):
            # Comskip returns 1 if no commercials found — that's OK
            logger.warning("Comskip returned %d: %s", result.returncode, result.stderr[:500])
    except FileNotFoundError:
        logger.warning("Comskip not found — skipping commercial detection")
        # Just copy the file
        import shutil
        shutil.copy2(source_path, output_path)
        return output_path, []
    except subprocess.TimeoutExpired:
        logger.error("Comskip timed out after 30 minutes")
        import shutil
        shutil.copy2(source_path, output_path)
        return output_path, []

    # Step 2: Parse EDL
    breaks = _parse_edl(edl_path)
    logger.info("Found %d commercial breaks", len(breaks))

    if not breaks:
        # No commercials detected — copy as-is
        import shutil
        shutil.copy2(source_path, output_path)
        return output_path, []

    # Step 3: Get video duration
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(source)
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    duration = float(json.loads(probe_result.stdout)["format"]["duration"])

    # Step 4: Build keep segments and re-encode
    keeps = _build_keep_segments(breaks, duration)
    logger.info(
        "Keeping %d segments (%.1fs content, %.1fs stripped)",
        len(keeps),
        sum(e - s for s, e in keeps),
        sum(e - s for s, e in breaks),
    )

    # Build FFmpeg concat filter
    # Create a segments file for concat demuxer
    segments_file = source.parent / "segments.txt"
    filter_parts = []
    for i, (start, end) in enumerate(keeps):
        filter_parts.append(
            f"between(t\\,{start:.3f}\\,{end:.3f})"
        )

    select_expr = "+".join(filter_parts)

    # Try NVENC first, fall back to libx265
    for encoder in ["hevc_nvenc", "libx265"]:
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", str(source),
            "-vf", f"select='{select_expr}',setpts=N/FRAME_RATE/TB",
            "-af", f"aselect='{select_expr}',asetpts=N/SR/TB",
            "-c:v", encoder,
            "-c:a", "aac",
            "-b:a", "192k",
            str(output),
        ]

        logger.info("Encoding with %s...", encoder)
        try:
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode == 0:
                logger.info("Re-encode complete: %s", output)
                return str(output), breaks
            logger.warning("FFmpeg %s failed: %s", encoder, result.stderr[:500])
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg timed out with %s", encoder)

    # If both encoders fail, fall back to copy
    logger.error("All encoders failed — copying source as-is")
    import shutil
    shutil.copy2(source_path, output_path)
    return output_path, breaks
