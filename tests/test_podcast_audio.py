"""Tests for podcast audio assembly and bed mixing."""

import json
import math
import re
import subprocess
import struct
import wave
from pathlib import Path

import pytest

from telegram_translator.podcast_generator import PodcastGenerator


def _write_tone(
    path: Path,
    duration: float,
    sample_rate: int = 8_000,
    amplitude: int = 4_000,
) -> None:
    """Write a small mono PCM tone fixture."""
    samples = [
        int(amplitude * math.sin(2 * math.pi * 220 * i / sample_rate))
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


def test_configured_pause_separates_voice_segments(tmp_path: Path):
    """The public pause setting must control non-topic segment spacing."""
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    output = tmp_path / "joined.wav"
    _write_tone(first, duration=0.5)
    _write_tone(second, duration=0.5)

    generator = PodcastGenerator(
        {
            "output_dir": str(tmp_path),
            "pause_between_segments_ms": 650,
            "audio": {
                "intro_bed": str(tmp_path / "missing-intro.wav"),
                "background_bed": str(tmp_path / "missing-background.wav"),
                "whoosh": str(tmp_path / "missing-whoosh.wav"),
                "lead_in_seconds": 0,
            },
        }
    )

    generator.assemble_podcast(
        [first, second],
        output,
        topic_boundaries=set(),
    )

    with wave.open(str(output), "rb") as result:
        assert result.getnframes() == int(1.65 * result.getframerate())


def test_background_bed_can_begin_after_intro_and_pause(tmp_path: Path):
    """A signature intro can finish before the underscore enters."""
    intro_voice = tmp_path / "intro-voice.wav"
    story_voice = tmp_path / "story-voice.wav"
    background = tmp_path / "background.wav"
    output = tmp_path / "delayed-background.wav"
    _write_silence(intro_voice, duration=0.5)
    _write_silence(story_voice, duration=0.5)
    _write_tone(background, duration=0.5)

    generator = PodcastGenerator(
        {
            "output_dir": str(tmp_path),
            "pause_between_segments_ms": 200,
            "audio": {
                "intro_bed": str(tmp_path / "missing-intro.wav"),
                "background_bed": str(background),
                "background_bed_start_after_intro": True,
                "background_bed_volume": 1.0,
                "background_fade_seconds": 0,
                "whoosh": str(tmp_path / "missing-whoosh.wav"),
                "lead_in_seconds": 0.1,
            },
        }
    )

    generator.assemble_podcast(
        [intro_voice, story_voice],
        output,
        topic_boundaries={1},
    )

    with wave.open(str(output), "rb") as result:
        samples = struct.unpack(
            f"<{result.getnframes()}h",
            result.readframes(result.getnframes()),
        )
        bed_start = int(0.8 * result.getframerate())
        assert result.getnframes() == int(1.3 * result.getframerate())
    assert not any(samples[:bed_start])
    assert any(samples[bed_start:])


def _measure_lufs(path: Path) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-19:TP=-2:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    measurements = re.findall(r"\{[^{}]+\}", result.stderr, re.DOTALL)
    return float(json.loads(measurements[-1])["input_i"])


def test_voice_normalization_equalizes_quiet_and_loud_sources(tmp_path: Path):
    """Source gain must not change narration level before bed mixing."""
    quiet = tmp_path / "quiet.wav"
    loud = tmp_path / "loud.wav"
    quiet_output = tmp_path / "quiet-mixed.wav"
    loud_output = tmp_path / "loud-mixed.wav"
    _write_tone(quiet, duration=5.0, amplitude=1_000)
    _write_tone(loud, duration=5.0, amplitude=10_000)

    generator = PodcastGenerator(
        {
            "output_dir": str(tmp_path),
            "audio": {
                "intro_bed": str(tmp_path / "missing-intro.wav"),
                "background_bed": str(tmp_path / "missing-background.wav"),
                "whoosh": str(tmp_path / "missing-whoosh.wav"),
                "lead_in_seconds": 0,
                "voice_target_lufs": -19,
            },
        }
    )

    generator.assemble_podcast([quiet], quiet_output, topic_boundaries=set())
    generator.assemble_podcast([loud], loud_output, topic_boundaries=set())

    quiet_lufs = _measure_lufs(quiet_output)
    loud_lufs = _measure_lufs(loud_output)
    with wave.open(str(quiet_output), "rb") as result:
        assert result.getframerate() == 8_000
    assert quiet_lufs == pytest.approx(-19, abs=0.2)
    assert loud_lufs == pytest.approx(-19, abs=0.2)
    assert quiet_lufs == pytest.approx(loud_lufs, abs=0.1)


@pytest.mark.parametrize("target", [-37, -8])
def test_voice_normalization_rejects_unsafe_target(tmp_path: Path, target: int):
    with pytest.raises(ValueError, match="between -36 and -9"):
        PodcastGenerator(
            {
                "output_dir": str(tmp_path),
                "audio": {"voice_target_lufs": target},
            }
        )


def test_voice_normalization_rejects_non_numeric_target(tmp_path: Path):
    with pytest.raises(ValueError, match="must be a number"):
        PodcastGenerator(
            {
                "output_dir": str(tmp_path),
                "audio": {"voice_target_lufs": "studio"},
            }
        )
