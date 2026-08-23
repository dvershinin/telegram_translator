"""Structural guards for the unattended daily podcast runner."""

import os
from pathlib import Path
import sqlite3
import subprocess


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "daily_podcasts.sh"
).read_text(encoding="utf-8")


def test_scalable_stories_runs_before_russian_podcast() -> None:
    """The English WordPress show should run before the Russian pipeline."""
    assert SCRIPT.index("run_podcast scalable_stories") < SCRIPT.index(
        "run_podcast vaske_daily"
    )


def test_wordpress_credentials_are_scoped_to_scalable_stories() -> None:
    """Cron retrieves the app password from Keychain and removes it afterward."""
    assert 'if [ "$name" = "scalable_stories" ]' in SCRIPT
    assert '"$SECURITY_BIN" find-generic-password' in SCRIPT
    assert "-s getpagespeed-scalable-stories-wordpress -w" in SCRIPT
    assert '"$KEYCHAIN_FILE"' in SCRIPT
    assert "export GPS_WP_USER=danila" in SCRIPT
    assert "export GPS_WP_APP_PASSWORD" in SCRIPT
    assert "unset GPS_WP_USER GPS_WP_APP_PASSWORD" in SCRIPT


def test_scheduled_runner_retries_safely_and_alerts_on_failure() -> None:
    """Failures stay visible while hourly retries cannot overlap or duplicate."""
    assert '/usr/bin/shlock -p "$$" -f "$LOCK_FILE"' in SCRIPT
    assert '"$SUCCESS_FILE"' in SCRIPT
    assert "human_action_alert" in SCRIPT
    assert "telegram-translator-daily-podcasts-$today" in SCRIPT
    assert "working_directory=$PROJECT_DIR" in SCRIPT
    assert 'if [ -n "$PIPELINE_FAILURES" ]' in SCRIPT
    assert "alert_failures || true" in SCRIPT
    assert "return 1" in SCRIPT
    assert 'podcast_already_published "$name" "$today"' in SCRIPT


def test_keychain_failure_is_recorded_and_uses_explicit_file(tmp_path: Path) -> None:
    """A cron-context Keychain miss remains retryable and names the exact file."""
    capture = tmp_path / "security-args"
    security = tmp_path / "security"
    security.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE"\nexit 1\n',
        encoding="utf-8",
    )
    security.chmod(0o755)
    env = os.environ | {"CAPTURE": str(capture)}
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; SECURITY_BIN="$2"; KEYCHAIN_FILE="$3"; '
            'CONTENT_DB="$4"; '
            'run_podcast scalable_stories; printf "%s" "$PIPELINE_FAILURES"',
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(security),
            "/tmp/login.keychain-db",
            str(tmp_path / "missing-content.sqlite"),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "scalable_stories (Keychain unavailable)" in result.stdout
    assert capture.read_text(encoding="utf-8").splitlines()[-1] == (
        "/tmp/login.keychain-db"
    )


def test_failure_alert_calls_system_mcp(tmp_path: Path) -> None:
    """The runner sends one typed local alert with retry context."""
    capture = tmp_path / "mcp-args"
    mcp_dev = tmp_path / "mcp-dev"
    mcp_dev.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE"\n', encoding="utf-8"
    )
    mcp_dev.chmod(0o755)
    env = os.environ | {"CAPTURE": str(capture)}
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; MCP_DEV="$2"; PIPELINE_FAILURES="scalable_stories"; '
            "alert_failures",
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(mcp_dev),
        ],
        check=True,
        env=env,
    )
    args = capture.read_text(encoding="utf-8")
    assert "human_action_alert" in args
    assert "Daily podcast pipeline failed" in args
    assert "retry hourly through 23:00" in args
    assert "working_directory=/Users/danila/Projects/telegram_translator" in args


def test_published_podcast_is_detected_from_authoritative_digest(
    tmp_path: Path,
) -> None:
    """A retry skips a show whose same-date digest is already published."""
    database = tmp_path / "content.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE digests (date TEXT, podcast_name TEXT, status TEXT)"
    )
    connection.execute(
        "INSERT INTO digests VALUES (?, ?, ?)",
        ("2026-08-24", "scalable_stories", "published"),
    )
    connection.commit()
    connection.close()

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; CONTENT_DB="$2"; '
            'podcast_already_published scalable_stories 2026-08-24',
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(database),
        ],
        check=False,
    )
    assert result.returncode == 0

    missing = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; CONTENT_DB="$2"; '
            'podcast_already_published crosswire 2026-08-24',
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(database),
        ],
        check=False,
    )
    assert missing.returncode == 1
