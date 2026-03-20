"""M4A (AAC) audio encoding for podcast publishing."""

import logging
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger(__name__)


def encode_m4a(
    wav_path: Path,
    m4a_path: Path,
    bitrate: str = "128k",
    metadata: dict | None = None,
) -> tuple[Path, float]:
    """Encode WAV to M4A (AAC) via pydub/ffmpeg.

    Args:
        wav_path: Path to the source WAV file.
        m4a_path: Path to write the M4A output.
        bitrate: AAC bitrate (e.g., "128k").
        metadata: Optional dict with keys: title, artist, album, date.

    Returns:
        Tuple of (m4a_path, duration_seconds).
    """
    audio = AudioSegment.from_wav(str(wav_path))
    duration_seconds = len(audio) / 1000.0

    m4a_path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(
        str(m4a_path),
        format="ipod",
        bitrate=bitrate,
        parameters=["-movflags", "+faststart"],
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
