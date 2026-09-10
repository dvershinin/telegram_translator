"""Regression tests for exact external-script digest ingestion."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from click.testing import CliRunner
import pytest

from telegram_translator.cli import cli
from telegram_translator.content_store import (
    ContentStore,
    DigestIngestConflictError,
)
from telegram_translator.digest import DigestPipeline


class _ConfigManagerStub:
    """Provide one external podcast and an isolated content database."""

    def __init__(self, database_path: Path, input_mode: str = "external_script"):
        self.database_path = database_path
        self.input_mode = input_mode

    def resolve_podcast_configs(self) -> dict:
        """Return the configured test podcast."""
        return {
            "morning": {
                "name": "morning",
                "input_mode": self.input_mode,
            }
        }

    def get_database_path(self, database_name: str) -> str:
        """Return the isolated database path."""
        assert database_name == "content_store.db"
        return str(self.database_path)


def test_store_preserves_exact_text_and_retries_idempotently(tmp_path: Path) -> None:
    """Preserve exact script bytes and reject a conflicting daily script."""
    store = ContentStore(tmp_path / "content.db")
    script = "  First line.\n\nSecond line with café.\n"

    digest, inserted = store.ingest_external_script("2026-09-10", "morning", script)
    assert inserted is True
    assert digest.podcast_script == script
    assert digest.executive_summary == script
    assert digest.status == "summarized"

    retried, inserted = store.ingest_external_script("2026-09-10", "morning", script)
    assert inserted is False
    assert retried.id == digest.id

    with pytest.raises(DigestIngestConflictError, match="different script"):
        store.ingest_external_script("2026-09-10", "morning", "Changed script")
    assert store.get_digest("2026-09-10", "morning").podcast_script == script


def test_store_invalidates_unpublished_artifacts_when_populating_stub(
    tmp_path: Path,
) -> None:
    """Clear stale unpublished artifacts when an empty digest is ingested."""
    store = ContentStore(tmp_path / "content.db")
    store.create_digest("2026-09-10", "morning")
    store.update_digest(
        "2026-09-10",
        "morning",
        audio_path="old.wav",
        m4a_path="old.m4a",
        duration_seconds=42,
        status="complete",
    )

    digest, inserted = store.ingest_external_script(
        "2026-09-10", "morning", "Exact script\n"
    )

    assert inserted is True
    assert digest.audio_path == ""
    assert digest.m4a_path == ""
    assert digest.duration_seconds == 0
    assert digest.published_at is None
    assert digest.status == "summarized"


def test_concurrent_conflicting_ingests_serialize(tmp_path: Path) -> None:
    """Allow only one of two concurrent scripts to claim the daily key."""
    store = ContentStore(tmp_path / "content.db")

    def ingest(script: str) -> str:
        try:
            store.ingest_external_script("2026-09-10", "morning", script)
            return "accepted"
        except DigestIngestConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(ingest, ["Script A", "Script B"]))

    assert sorted(outcomes) == ["accepted", "conflict"]
    stored = store.get_digest("2026-09-10", "morning")
    assert stored.podcast_script in {"Script A", "Script B"}


@pytest.mark.asyncio
async def test_targeted_news_pipeline_refuses_external_podcast(
    tmp_path: Path,
) -> None:
    """Keep generic news operations from overwriting an external script."""

    class PipelineConfig:
        """Provide one external-script podcast to DigestPipeline."""

        config = {"sources": {}}

        def get_database_path(self, database_name: str) -> str:
            """Return an isolated content database."""
            return str(tmp_path / database_name)

        def resolve_podcast_configs(self) -> dict:
            """Return the external podcast."""
            return {
                "morning": {
                    "name": "morning",
                    "input_mode": "external_script",
                    "sources": {},
                }
            }

    pipeline = DigestPipeline(PipelineConfig(), podcast_name="morning")

    for operation in (pipeline.collect, pipeline.summarize, pipeline.run):
        with pytest.raises(ValueError, match="digest ingest"):
            await operation("2026-09-10")


def test_cli_ingest_uses_stdin_without_news_or_model_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest stdin directly without invoking collection or summarization."""
    database_path = tmp_path / "content.db"
    manager = _ConfigManagerStub(database_path)
    monkeypatch.setattr("telegram_translator.cli.ConfigManager", lambda: manager)

    async def forbidden(*args, **kwargs):
        raise AssertionError("news/model pipeline must not run")

    monkeypatch.setattr("telegram_translator.digest.DigestPipeline.collect", forbidden)
    monkeypatch.setattr(
        "telegram_translator.digest.DigestPipeline.summarize", forbidden
    )

    script = "  Exact opening\n\nExact ending.\n"
    result = CliRunner().invoke(
        cli,
        [
            "digest",
            "ingest",
            "--podcast",
            "morning",
            "--date",
            "2026-09-10",
        ],
        input=script,
    )

    assert result.exit_code == 0, result.output
    stored = ContentStore(database_path).get_digest("2026-09-10", "morning")
    assert stored.podcast_script == script
    assert stored.executive_summary == script
    assert str(database_path) in result.output


@pytest.mark.parametrize(
    ("date", "script", "input_mode", "error"),
    [
        ("2026/09/10", "Script", "external_script", "YYYY-MM-DD"),
        ("2026-09-10", " \n\t", "external_script", "must not be empty"),
        ("2026-09-10", "Script", "news", "does not accept"),
    ],
)
def test_cli_ingest_rejects_invalid_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    date: str,
    script: str,
    input_mode: str,
    error: str,
) -> None:
    """Reject malformed dates, empty input, and ordinary news podcasts."""
    manager = _ConfigManagerStub(tmp_path / "content.db", input_mode)
    monkeypatch.setattr("telegram_translator.cli.ConfigManager", lambda: manager)

    result = CliRunner().invoke(
        cli,
        [
            "digest",
            "ingest",
            "--podcast",
            "morning",
            "--date",
            date,
        ],
        input=script,
    )

    assert result.exit_code != 0
    assert error in result.output


def test_cli_ingest_rejects_oversized_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject scripts larger than the 64 KiB byte limit."""
    manager = _ConfigManagerStub(tmp_path / "content.db")
    monkeypatch.setattr("telegram_translator.cli.ConfigManager", lambda: manager)

    result = CliRunner().invoke(
        cli,
        [
            "digest",
            "ingest",
            "--podcast",
            "morning",
            "--date",
            "2026-09-10",
        ],
        input="x" * 65537,
    )

    assert result.exit_code != 0
    assert "64 KiB" in result.output
