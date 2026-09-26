"""Publish gate: every TTS segment must transcribe back to its script.

Regression tests for the 2026-09-22 incident: a Voicebox regression
rendered the whole Crosswire 2026-09-22 episode as garbled non-speech
("Tick tock, tick tock..."), /generate reported success for every broken
segment, and the episode shipped to the public feed. Generation success
proves nothing about the audio — only an STT round-trip does.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from telegram_translator import podcast_generator
from telegram_translator.podcast_generator import (
    PodcastGenerator,
    _normalize_spoken_tokens,
    _verification_language,
    _word_error_rate,
)


# ---------------------------------------------------------------- helpers


def test_normalize_strips_formatting_but_keeps_digits():
    assert _normalize_spoken_tokens("Bitcoin trades at $112,485!") == [
        "bitcoin", "trades", "at", "112", "485",
    ]


def test_wer_zero_for_identical():
    tokens = _normalize_spoken_tokens("Good morning, it's September 22nd.")
    assert _word_error_rate(tokens, tokens) == 0.0


def test_wer_high_for_garbled():
    ref = _normalize_spoken_tokens(
        "Good morning, it's September 22nd. Markets open in 45 minutes."
    )
    hyp = _normalize_spoken_tokens("Tick tock, tick tock. Peace out yo!")
    assert _word_error_rate(ref, hyp) > 0.8


def test_wer_counts_hallucinated_insertions():
    ref = ["hello"]
    hyp = ["hello", "tick", "tock", "tick", "tock"]
    assert _word_error_rate(ref, hyp) > 1.0


# ------------------------------------------------- verification behavior


def _ok_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


class _ScriptedClient:
    """Generation returns success; /transcribe replies come from a script."""

    def __init__(self, transcripts):
        self._transcripts = list(transcripts)
        self.generation_calls = 0
        self.transcribe_calls = 0
        self.transcribe_languages = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, files=None, data=None):
        if files is not None:
            self.transcribe_calls += 1
            self.transcribe_languages.append(
                (data or {}).get("language", "<omitted>")
            )
            if not self._transcripts:
                pytest.fail("unexpected extra transcribe call")
            item = self._transcripts.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = item((data or {}).get("language", "<omitted>"))
            return _ok_response({"text": item})
        self.generation_calls += 1
        return _ok_response({"id": f"gen-{self.generation_calls}", "duration": 1.0})

    async def get(self, url):
        response = _ok_response(None)
        response.content = b"RIFF-placeholder"
        return response


def _generator(tmp_path, **extra):
    config = {
        "name": "test",
        "voicebox_url": "http://localhost:17493",
        "voice_profile": "profile-id",
        "output_dir": str(tmp_path),
        "segment_max_attempts": 3,
        **extra,
    }
    generator = PodcastGenerator(
        config, tts_cache_dir=tmp_path / "tts_cache"
    )
    generator._profile_id = "profile-id"
    return generator


def _patch(monkeypatch, client):
    monkeypatch.setattr(
        podcast_generator.httpx, "AsyncClient", lambda *a, **k: client
    )

    async def _no_sleep(seconds):
        pass

    monkeypatch.setattr(podcast_generator.asyncio, "sleep", _no_sleep)


TEXT = "Good morning, it's September 22nd. Markets open in 45 minutes."


@pytest.mark.asyncio
async def test_verified_segment_passes_and_is_cached(monkeypatch, tmp_path):
    client = _ScriptedClient([TEXT])
    _patch(monkeypatch, client)
    generator = _generator(tmp_path)

    out = await generator.generate_segment(TEXT, tmp_path / "seg.wav")

    assert out.exists()
    assert client.transcribe_calls == 1
    assert client.transcribe_languages == ["en"]
    assert generator._tts_cache_path(TEXT).exists()


@pytest.mark.asyncio
async def test_garbled_audio_is_regenerated_then_fails_episode(
    monkeypatch, tmp_path
):
    garbage = "Tick tock, tick tock. Peace out yo!"
    client = _ScriptedClient([garbage, garbage, garbage])
    _patch(monkeypatch, client)
    generator = _generator(tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        await generator.generate_segment(TEXT, tmp_path / "seg.wav")

    assert client.generation_calls == 3  # every attempt regenerated
    assert "word error rate" in str(excinfo.value)
    # Garbage must never enter the TTS cache.
    assert not generator._tts_cache_path(TEXT).exists()


@pytest.mark.asyncio
async def test_garbled_then_good_self_heals(monkeypatch, tmp_path):
    client = _ScriptedClient(["Tick tock, tick tock.", TEXT])
    _patch(monkeypatch, client)
    generator = _generator(tmp_path)

    out = await generator.generate_segment(TEXT, tmp_path / "seg.wav")

    assert out.exists()
    assert client.generation_calls == 2


@pytest.mark.asyncio
async def test_silent_audio_fails_verification(monkeypatch, tmp_path):
    client = _ScriptedClient(["", "", ""])
    _patch(monkeypatch, client)
    generator = _generator(tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        await generator.generate_segment(TEXT, tmp_path / "seg.wav")

    assert "no speech recognized" in str(excinfo.value)


@pytest.mark.asyncio
async def test_transcription_outage_fails_closed(monkeypatch, tmp_path):
    error = httpx.ConnectError("transcribe down")
    client = _ScriptedClient([error, error, error])
    _patch(monkeypatch, client)
    generator = _generator(tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        await generator.generate_segment(TEXT, tmp_path / "seg.wav")

    assert "transcription unavailable" in str(excinfo.value)
    assert not generator._tts_cache_path(TEXT).exists()


@pytest.mark.asyncio
async def test_verification_can_be_disabled(monkeypatch, tmp_path):
    client = _ScriptedClient([])
    _patch(monkeypatch, client)
    generator = _generator(tmp_path, tts_verify=False)

    out = await generator.generate_segment(TEXT, tmp_path / "seg.wav")

    assert out.exists()
    assert client.transcribe_calls == 0


# ------------------------------------ mixed-language segments (2026-09-25)
#
# The morning brief mixes Russian and English by design. Its podcast
# language is "en", and the check forced that hint onto Whisper, which
# then TRANSLATED the Russian half instead of transcribing it: WER 0.71
# on perfectly good audio, four regenerations per run, and TGP re-ran the
# stuck date every ten minutes for two days. Fixtures below are the real
# 25.09 segment 5 and Whisper's real transcripts of its audio.

MIXED_SEGMENT = (
    "В остальном: Meta представила VR-шлем весом 100 граммов примерно за "
    "1,300 долларов и очки Ray-Ban без камеры за 349, явно чтобы снять "
    "backlash по поводу скрытой записи. Microsoft обновила Surface на новом "
    "Snapdragon X2 Plus, и базовые 8 гигабайт памяти ушли в прошлое, теперь "
    "минимум 16. Personal and health. You're in Pakulonan Barat, local time "
    "just past 8:27, 30 degrees, and your location fix is about 16 hours "
    "old. The health flag today is sleep: one hour recorded, which is poor."
)
WHISPER_FORCED_EN = (
    "In the rest, Meta presented a VR helmet weighing 100 grams for about "
    "$ 1,300 and Ray-Ban glasses without a camera for $ 349, clearly to "
    "remove the bug only about the hidden recording. Microsoft has updated "
    "Surface with the new Snapdragon X2 Plus, and the basic 8 GB of memory "
    "has gone into the past, now at least 16. The temperature is 28.27, 30 "
    "degrees, and your location fix is about 16 hours old. The health flag "
    "today is sleep, 1 hour recorded, which is poor."
)
WHISPER_FAITHFUL = (
    "В остальном, Meta представила VR-шлем весом 100 граммов примерно за "
    "1.300 долларов и очки Ray-Ban без камеры за 349, явно чтобы снять "
    "баклиш по поводу скрытой записи. Microsoft обновила Surface на новом "
    "Snapdragon X2 Plus и базовые 8 гигабайт памяти ушли в прошлое, теперь "
    "минимум 16. Personal and Health, you're in Pakolonen Barat, local time "
    "just passed 8.27, 30 degrees, and your location fix is about 16 hours "
    "old. The health flag today is sleep, one hour recorded, which is poor."
)


def _whisper(language):
    """Real Whisper: forcing English onto Russian speech translates it."""
    return WHISPER_FORCED_EN if language == "en" else WHISPER_FAITHFUL


def test_verification_language_keeps_hint_for_single_language_segments():
    assert _verification_language(TEXT, "en") == "en"
    assert _verification_language("Доброе утро, рынок открыт.", "ru") == "ru"
    # Latin brand names inside a Russian show do not make it mixed.
    assert _verification_language("Meta выпустила Ray-Ban.", "ru") == "ru"


def test_verification_language_auto_detects_cyrillic_in_non_russian_show():
    assert _verification_language(MIXED_SEGMENT, "en") is None


@pytest.mark.asyncio
async def test_mixed_language_segment_verifies_without_forced_english(
    monkeypatch, tmp_path
):
    client = _ScriptedClient([_whisper] * 3)
    _patch(monkeypatch, client)
    generator = _generator(tmp_path, language="en")

    out = await generator.generate_segment(MIXED_SEGMENT, tmp_path / "seg.wav")

    assert out.exists()
    assert client.generation_calls == 1
    # The hint is left out entirely (httpx rejects None form values).
    assert client.transcribe_languages == ["<omitted>"]
