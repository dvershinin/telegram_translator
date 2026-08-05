"""Tests for M4A encoder options."""

from pathlib import Path
from types import SimpleNamespace

from telegram_translator.audio_encoder import encode_m4a


class _FakeAudio:
    """Minimal AudioSegment stand-in that records export arguments."""

    def __init__(self):
        self.export_kwargs = None

    def __len__(self):
        return 1000

    def export(self, destination, **kwargs):
        self.export_kwargs = {"destination": destination, **kwargs}


def test_loudness_normalization_is_opt_in(monkeypatch, tmp_path):
    """A configured target adds loudnorm without changing other exports."""
    audio = _FakeAudio()
    monkeypatch.setattr(
        "telegram_translator.audio_encoder.AudioSegment.from_wav",
        lambda path: audio,
    )
    monkeypatch.setattr(
        "telegram_translator.audio_encoder.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stderr='''
            {
                "input_i": "-29.40",
                "input_tp": "-5.20",
                "input_lra": "6.10",
                "input_thresh": "-39.60",
                "target_offset": "0.00"
            }
            ''',
        ),
    )
    destination = tmp_path / "episode.m4a"

    path, duration = encode_m4a(
        Path("episode.wav"),
        destination,
        "96k",
        loudness_target_lufs=-19,
    )

    assert path == destination
    assert duration == 1.0
    assert audio.export_kwargs == {
        "destination": str(destination),
        "format": "ipod",
        "bitrate": "96k",
        "parameters": [
            "-af",
            (
                "loudnorm=I=-19:TP=-2:LRA=11:"
                "measured_I=-29.40:measured_TP=-5.20:"
                "measured_LRA=6.10:measured_thresh=-39.60:"
                "offset=0.00:linear=true:print_format=summary"
            ),
            "-movflags",
            "+faststart",
        ],
    }
