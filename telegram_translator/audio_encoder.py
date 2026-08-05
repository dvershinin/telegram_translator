"""M4A (AAC) audio encoding for podcast publishing."""

import json
import logging
import re
import subprocess
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger(__name__)


def _measured_loudnorm_filter(
    wav_path: Path,
    target_lufs: float,
) -> str:
    """Build a second-pass EBU R128 filter from an ffmpeg measurement.

    Args:
        wav_path: Source WAV file to analyze.
        target_lufs: Desired integrated loudness in LUFS.

    Returns:
        An ffmpeg loudnorm filter string containing measured input values.

    Raises:
        RuntimeError: If ffmpeg fails or returns unusable measurements.
    """
    target = f"{float(target_lufs):g}"
    analysis_filter = (
        f"loudnorm=I={target}:TP=-2:LRA=11:print_format=json"
    )
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(wav_path),
                "-af",
                analysis_filter,
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("ffmpeg loudness analysis failed") from exc

    matches = re.findall(r"\{[^{}]+\}", result.stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg loudness analysis returned no measurements")
    try:
        measured = json.loads(matches[-1])
        values = {
            "measured_I": measured["input_i"],
            "measured_TP": measured["input_tp"],
            "measured_LRA": measured["input_lra"],
            "measured_thresh": measured["input_thresh"],
            "offset": measured["target_offset"],
        }
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("ffmpeg loudness measurements were invalid") from exc

    measurements = ":".join(
        f"{key}={value}" for key, value in values.items()
    )
    return (
        f"loudnorm=I={target}:TP=-2:LRA=11:{measurements}:"
        "linear=true:print_format=summary"
    )


def encode_m4a(
    wav_path: Path,
    m4a_path: Path,
    bitrate: str = "128k",
    metadata: dict | None = None,
    loudness_target_lufs: float | None = None,
) -> tuple[Path, float]:
    """Encode WAV to M4A (AAC) via pydub/ffmpeg.

    Args:
        wav_path: Path to the source WAV file.
        m4a_path: Path to write the M4A output.
        bitrate: AAC bitrate (e.g., "128k").
        metadata: Optional dict with keys: title, artist, album, date.
        loudness_target_lufs: Optional integrated loudness target. When set,
            ffmpeg applies EBU R128 loudness normalization during encoding.

    Returns:
        Tuple of (m4a_path, duration_seconds).
    """
    audio = AudioSegment.from_wav(str(wav_path))
    duration_seconds = len(audio) / 1000.0

    m4a_path.parent.mkdir(parents=True, exist_ok=True)
    parameters = ["-movflags", "+faststart"]
    if loudness_target_lufs is not None:
        parameters = [
            "-af",
            _measured_loudnorm_filter(wav_path, loudness_target_lufs),
            *parameters,
        ]

    audio.export(
        str(m4a_path),
        format="ipod",
        bitrate=bitrate,
        parameters=parameters,
    )

    if metadata:
        from mutagen.mp4 import MP4

        mp4 = MP4(str(m4a_path))
        tag_map = {
            "title": "\xa9nam",
            "artist": "\xa9ART",
            "album": "\xa9alb",
            "date": "\xa9day",
        }
        for key, atom in tag_map.items():
            if key in metadata:
                mp4[atom] = [metadata[key]]
        mp4.save()

    logger.info(
        "Encoded %s -> %s (%.1fs, %s)",
        wav_path.name,
        m4a_path.name,
        duration_seconds,
        bitrate,
    )
    return m4a_path, duration_seconds
