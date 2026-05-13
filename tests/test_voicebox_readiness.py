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
