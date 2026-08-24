"""Tests for podcast audio assembly and bed mixing."""

import math
import struct
import wave
from pathlib import Path

from telegram_translator.podcast_generator import PodcastGenerator


def _write_tone(path: Path, duration: float, sample_rate: int = 8_000) -> None:
    """Write a small mono PCM tone fixture."""
    samples = [
        int(4_000 * math.sin(2 * math.pi * 220 * i / sample_rate))
        for i in range(int(duration * sample_rate))
    ]
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _write_silence(path: Path, duration: float, sample_rate: int = 8_000) -> None:
    """Write a small mono PCM silence fixture."""
    sample_count = int(duration * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * sample_count)


def test_single_segment_still_receives_lead_in_and_audio_beds(tmp_path: Path):
    """A one-segment preview must use the same mixer as a full episode."""
    voice = tmp_path / "voice.wav"
    intro = tmp_path / "intro.wav"
    background = tmp_path / "background.wav"
    output = tmp_path / "mixed.wav"
    _write_silence(voice, duration=0.5)
    _write_tone(intro, duration=0.2)
    _write_tone(background, duration=0.5)

    generator = PodcastGenerator(
        {
            "output_dir": str(tmp_path),
            "audio": {
                "intro_bed": str(intro),
                "background_bed": str(background),
                "whoosh": str(tmp_path / "missing-whoosh.wav"),
                "lead_in_seconds": 0.1,
                "intro_fade_seconds": 0.05,
                "intro_bed_volume": 1.0,
                "background_bed_volume": 1.0,
                "background_fade_seconds": 0.0,
            },
        }
    )

    generator.assemble_podcast([voice], output, topic_boundaries=set())

    with wave.open(str(output), "rb") as result:
        samples = struct.unpack(
            f"<{result.getnframes()}h",
            result.readframes(result.getnframes()),
        )
        assert result.getnframes() == int(0.6 * result.getframerate())
    assert any(samples)
