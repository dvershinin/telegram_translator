"""Regression tests for the Voicebox readiness retry loop.

Background: under brew, voicebox runs via launchd socket activation
with a 30-minute idle exit. Cold-start clients used to fail with
``httpx.ReadTimeout`` because the FastAPI startup hook ``await``-ed
TTS model load before answering ``/profiles``. The server-side fix
backgrounds the preload; this client-side retry loop is the
defense-in-depth that keeps daily cron resilient to launchd spawn
jitter or a future regression of the same shape.

These tests pin the contract of ``_fetch_profiles_with_retry``:
  * first-try success returns the profile list with no retry,
  * transient HTTP errors are retried within the wall-clock budget,
  * a fully unreachable backend raises ``RuntimeError`` with the
    "Cannot reach Voicebox" wording (caller semantics).
"""

from unittest.mock import MagicMock

import httpx
import pytest

from telegram_translator import podcast_generator
from telegram_translator.podcast_generator import PodcastGenerator


def _make_generator(tmp_path) -> PodcastGenerator:
    """Build a minimal PodcastGenerator pointing at a writable tmp dir."""
    config = {
        "name": "test",
        "voicebox_url": "http://localhost:17493",
        "voice_profile": "test-profile",
        "output_dir": str(tmp_path),
        "audio": {},
    }
    return PodcastGenerator(config)


def _ok_response(payload):
    """Mock httpx.Response with a JSON body and a no-op raise_for_status."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


class _ScriptedAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient.

    Pops one entry per ``client.get`` call from ``responses``. Entries
    that are ``Exception`` instances are raised; everything else is
    returned. Re-entered across retry iterations so a single instance
    spans the entire test scenario.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.calls += 1
        if not self._responses:
            pytest.fail(f"unexpected extra request to {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_async_client(monkeypatch, fake):
    """Make every ``httpx.AsyncClient(...)`` return the same scripted fake."""
    monkeypatch.setattr(
        podcast_generator.httpx,
        "AsyncClient",
        lambda *args, **kwargs: fake,
    )


def _patch_sleep(monkeypatch):
    """Replace ``asyncio.sleep`` so the retry loop runs instantly."""
    sleeps: list[float] = []

    async def _no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(podcast_generator.asyncio, "sleep", _no_sleep)
    return sleeps


class _GenerationClient:
    """Capture one generation payload and return a small WAV placeholder."""

    def __init__(self):
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        self.payload = json
        return _ok_response({"id": "generation-1", "duration": 1.0})

    async def get(self, url):
        response = _ok_response(None)
        response.content = b"RIFF-placeholder"
        return response


@pytest.mark.asyncio
async def test_fetch_profiles_succeeds_on_first_try(monkeypatch, tmp_path):
    """No retry, no sleep when the very first GET returns 200."""
    payload = [{"id": "abc", "name": "Test"}]
    fake = _ScriptedAsyncClient([_ok_response(payload)])
    _patch_async_client(monkeypatch, fake)
    sleeps = _patch_sleep(monkeypatch)

    gen = _make_generator(tmp_path)
    result = await gen._fetch_profiles_with_retry()

    assert result == payload
    assert fake.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_fetch_profiles_retries_then_succeeds(monkeypatch, tmp_path):
    """Two ConnectErrors then a 200 — caller sees only the final payload."""
    payload = [{"id": "abc", "name": "Test"}]
    fake = _ScriptedAsyncClient([
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("upstream still loading"),
        _ok_response(payload),
    ])
    _patch_async_client(monkeypatch, fake)
    sleeps = _patch_sleep(monkeypatch)

    gen = _make_generator(tmp_path)
    result = await gen._fetch_profiles_with_retry()

    assert result == payload
    assert fake.calls == 3
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_fetch_profiles_raises_after_budget(monkeypatch, tmp_path):
    """Budget exhausted -> RuntimeError preserving caller-visible wording."""
    fake = _ScriptedAsyncClient([httpx.ConnectError("nope")] * 100)
    _patch_async_client(monkeypatch, fake)
    _patch_sleep(monkeypatch)

    # Jump the monotonic clock past the 30 s budget on the second read so
    # the loop performs exactly one attempt and then bails — without this,
    # the test would sit through 15 iterations of (fake) sleep.
    counter = {"n": 0}

    def _fake_monotonic():
        counter["n"] += 1
        return 0.0 if counter["n"] == 1 else 1000.0

    monkeypatch.setattr(podcast_generator.time, "monotonic", _fake_monotonic)

    gen = _make_generator(tmp_path)
    with pytest.raises(RuntimeError, match="Cannot reach Voicebox"):
        await gen._fetch_profiles_with_retry()
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_get_profile_id_uses_retry_path(monkeypatch, tmp_path):
    """_get_profile_id resolves a name match through the retry helper."""
    payload = [
        {"id": "uuid-1", "name": "Other Profile"},
        {"id": "uuid-2", "name": "test-profile"},
    ]
    fake = _ScriptedAsyncClient([
        httpx.ConnectError("cold spawn"),
        _ok_response(payload),
    ])
    _patch_async_client(monkeypatch, fake)
    _patch_sleep(monkeypatch)

    gen = _make_generator(tmp_path)
    profile_id = await gen._get_profile_id()

    assert profile_id == "uuid-2"
    assert fake.calls == 2
    # Second call short-circuits via the cached attribute.
    profile_id_again = await gen._get_profile_id()
    assert profile_id_again == "uuid-2"
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_generate_segment_forwards_voice_instruction(monkeypatch, tmp_path):
    """The selected delivery direction must reach Voicebox unchanged."""
    instruction = "Speak naturally with restrained emotion."
    generator = PodcastGenerator(
        {
            "name": "test",
            "voicebox_url": "http://localhost:17493",
            "voice_profile": "profile-id",
            "voice_instruct": instruction,
            "output_dir": str(tmp_path),
        }
    )
    generator._profile_id = "profile-id"
    client = _GenerationClient()
    monkeypatch.setattr(
        podcast_generator.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client,
    )

    await generator.generate_segment("Hello.", tmp_path / "segment.wav")

    assert client.payload["instruct"] == instruction


class _FlakyGenerationClient:
    """Fail the POST ``fail_times`` times, then succeed.

    Each ``httpx.AsyncClient(...)`` call re-enters the same instance, so
    the failure counter spans the whole retry loop.
    """

    def __init__(self, error, fail_times):
        self._error = error
        self._fail_times = fail_times
        self.post_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        self.post_calls += 1
        if self.post_calls <= self._fail_times:
            raise self._error
        return _ok_response({"id": "generation-1", "duration": 1.0})

    async def get(self, url):
        response = _ok_response(None)
        response.content = b"RIFF-placeholder"
        return response


def _segment_generator(tmp_path):
    generator = PodcastGenerator(
        {
            "name": "test",
            "voicebox_url": "http://localhost:17493",
            "voice_profile": "profile-id",
            "output_dir": str(tmp_path),
            "segment_max_attempts": 4,
        }
    )
    generator._profile_id = "profile-id"
    return generator


@pytest.mark.asyncio
async def test_generate_segment_retries_transient_timeout(monkeypatch, tmp_path):
    """A connection/timeout blip mid-episode must self-heal, not abort."""
    sleeps = _patch_sleep(monkeypatch)
    generator = _segment_generator(tmp_path)
    client = _FlakyGenerationClient(httpx.ReadTimeout("upstream busy"), fail_times=2)
    _patch_async_client(monkeypatch, client)

    out = await generator.generate_segment("Hello.", tmp_path / "seg.wav")

    assert out.exists()
    assert client.post_calls == 3  # two failures + one success
    assert len(sleeps) == 2  # one backoff before each retry


@pytest.mark.asyncio
async def test_generate_segment_retries_transient_5xx(monkeypatch, tmp_path):
    """A 503 from Voicebox is transient and must be retried."""
    _patch_sleep(monkeypatch)
    generator = _segment_generator(tmp_path)
    resp = MagicMock()
    resp.status_code = 503
    resp.text = "service unavailable"
    error = httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
    client = _FlakyGenerationClient(error, fail_times=1)
    _patch_async_client(monkeypatch, client)

    out = await generator.generate_segment("Hello.", tmp_path / "seg.wav")

    assert out.exists()
    assert client.post_calls == 2


@pytest.mark.asyncio
async def test_generate_segment_does_not_retry_deterministic_4xx(monkeypatch, tmp_path):
    """A 400 (bad text/profile) is deterministic — fail fast, no retries."""
    sleeps = _patch_sleep(monkeypatch)
    generator = _segment_generator(tmp_path)
    resp = MagicMock()
    resp.status_code = 400
    resp.text = "bad request"
    error = httpx.HTTPStatusError("400", request=MagicMock(), response=resp)
    client = _FlakyGenerationClient(error, fail_times=99)
    _patch_async_client(monkeypatch, client)

    with pytest.raises(RuntimeError) as excinfo:
        await generator.generate_segment(
            "Hello.", tmp_path / "seg.wav", segment_index=3
        )

    assert client.post_calls == 1  # no retry on a 4xx
    assert sleeps == []
    # The error must name the exact segment and HTTP status at a glance.
    message = str(excinfo.value)
    assert "test segment 3" in message
    assert "HTTP 400" in message
    assert "bad request" in message


@pytest.mark.asyncio
async def test_generate_segment_raises_after_exhausting_attempts(monkeypatch, tmp_path):
    """Persistent transient failure exhausts the budget, then raises."""
    _patch_sleep(monkeypatch)
    generator = _segment_generator(tmp_path)
    client = _FlakyGenerationClient(httpx.ConnectError("down"), fail_times=99)
    _patch_async_client(monkeypatch, client)

    with pytest.raises(RuntimeError) as excinfo:
        await generator.generate_segment(
            "Hello.", tmp_path / "seg.wav", segment_index=7
        )

    assert client.post_calls == 4  # segment_max_attempts
    # A bodyless transport error made the 2026-09-07 failure unpinnable:
    # the message must carry the segment, attempts, elapsed time, and the
    # exact exception type/repr.
    message = str(excinfo.value)
    assert "test segment 7" in message
    assert "after 4 attempts" in message
    assert "ConnectError" in message
    assert "down" in message


def test_voice_instruction_participates_in_tts_cache_key(tmp_path):
    """Changing delivery direction must not reuse a stale TTS segment."""
    base = {
        "name": "test",
        "voice_profile": "profile-id",
        "output_dir": str(tmp_path),
    }
    plain = PodcastGenerator(base, tts_cache_dir=tmp_path / "cache")
    directed = PodcastGenerator(
        {**base, "voice_instruct": "Speak naturally."},
        tts_cache_dir=tmp_path / "cache",
    )

    assert plain._tts_cache_path("Hello.") != directed._tts_cache_path("Hello.")
