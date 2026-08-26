"""Structural guards for the unattended daily podcast runner."""

import os
from pathlib import Path
import sqlite3
import subprocess


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "daily_podcasts.sh"
).read_text(encoding="utf-8")
CRON_ENTRY = Path(__file__).resolve().parents[1] / "scripts" / "daily_podcasts.cron"


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


def test_scheduled_runner_is_safe_and_alerts_on_failure() -> None:
    """Failures stay visible while duplicate invocations cannot overlap."""
    assert 'SHLOCK_BIN="${SHLOCK_BIN:-/usr/bin/shlock}"' in SCRIPT
    assert '"$SHLOCK_BIN" -p "$$" -f "$LOCK_FILE"' in SCRIPT
    assert '"$SUCCESS_FILE"' in SCRIPT
    assert "human_action_alert" in SCRIPT
    assert "telegram-translator-daily-podcasts-$run_date" in SCRIPT
    assert "working_directory=$PROJECT_DIR" in SCRIPT
    assert 'if [ -n "$PIPELINE_FAILURES" ]' in SCRIPT
    assert 'alert_failures "$run_date" || true' in SCRIPT
    assert "return 1" in SCRIPT
    assert 'podcast_already_published "$name" "$run_date"' in SCRIPT


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
            'run_podcast scalable_stories 2026-08-25; '
            'printf "%s" "$PIPELINE_FAILURES"',
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
            "alert_failures 2026-08-25",
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
    assert "re-run that date after fixing the cause" in args
    assert "working_directory=/Users/danila/Projects/telegram_translator" in args


def test_published_podcast_is_detected_from_authoritative_digest(
    tmp_path: Path,
) -> None:
    """A retry skips a show whose same-date digest is already published."""
    database = tmp_path / "content.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE digests (date TEXT, podcast_name TEXT, status TEXT, "
        "published_at TEXT, m4a_path TEXT)"
    )
    artifact = tmp_path / "episode.m4a"
    artifact.write_bytes(b"m4a")
    connection.execute(
        "INSERT INTO digests VALUES (?, ?, ?, ?, ?)",
        (
            "2026-08-24",
            "scalable_stories",
            "published",
            "2026-08-24T01:00:00+00:00",
            str(artifact),
        ),
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


def test_main_pins_one_logical_date_across_midnight(tmp_path: Path) -> None:
    """Every stage keeps the date captured once when the runner starts."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".secrets").write_text("", encoding="utf-8")
    security = tmp_path / "security"
    security.write_text("#!/bin/bash\nprintf 'test-password\\n'\n", encoding="utf-8")
    security.chmod(0o755)
    capture = tmp_path / "calls"
    date_called = tmp_path / "date-called"
    state_dir = tmp_path / "state"

    shell = r'''
source "$1"
STATE_DIR="$2"
SUCCESS_FILE="$STATE_DIR/success"
LOCK_FILE="$STATE_DIR/lock"
CONTENT_DB="$STATE_DIR/content.sqlite"
SECURITY_BIN="$3"
CAPTURE="$4"
DATE_CALLED="$5"
SHLOCK_BIN=/usr/bin/true
date() {
    if [ -e "$DATE_CALLED" ]; then
        printf '2026-08-26\n'
    else
        : > "$DATE_CALLED"
        printf '2026-08-25\n'
    fi
}
curl() { return 1; }
podcast_already_published() {
    printf 'skip %s %s\n' "$1" "$2" >> "$CAPTURE"
    return 1
}
mock_cli() { printf 'cli %s\n' "$*" >> "$CAPTURE"; }
CLI=mock_cli
main
'''
    subprocess.run(
        [
            "bash",
            "-c",
            shell,
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(state_dir),
            str(security),
            str(capture),
            str(date_called),
        ],
        check=True,
        env=os.environ | {"HOME": str(fake_home)},
    )

    calls = capture.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "cli digest collect --date 2026-08-25"
    assert all("2026-08-25" in call for call in calls)
    assert not any("2026-08-26" in call for call in calls)
    assert (state_dir / "success").read_text(encoding="utf-8") == "2026-08-25\n"


def test_retry_skip_uses_publication_fact_and_local_artifact(
    tmp_path: Path,
) -> None:
    """Static, Astro, and WordPress success share one retry contract."""
    database = tmp_path / "content.sqlite"
    artifacts = {
        name: tmp_path / f"{name}.m4a"
        for name in ("static_show", "astro_show", "wordpress_show")
    }
    for artifact in artifacts.values():
        artifact.write_bytes(b"m4a")

    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE digests ("
        "date TEXT, podcast_name TEXT, status TEXT, "
        "published_at TEXT, m4a_path TEXT)"
    )
    connection.executemany(
        "INSERT INTO digests VALUES (?, ?, ?, ?, ?)",
        [
            (
                "2026-08-25",
                "static_show",
                "complete",
                "2026-08-25T01:00:00+00:00",
                str(artifacts["static_show"]),
            ),
            (
                "2026-08-25",
                "astro_show",
                "complete",
                "2026-08-25T02:00:00+00:00",
                str(artifacts["astro_show"]),
            ),
            (
                "2026-08-25",
                "wordpress_show",
                "published",
                "2026-08-25T03:00:00+00:00",
                str(artifacts["wordpress_show"]),
            ),
            (
                "2026-08-25",
                "missing_artifact",
                "complete",
                "2026-08-25T04:00:00+00:00",
                str(tmp_path / "missing.m4a"),
            ),
            (
                "2026-08-25",
                "not_published",
                "complete",
                None,
                str(artifacts["static_show"]),
            ),
        ],
    )
    connection.commit()
    connection.close()

    def skip_result(podcast_name: str) -> int:
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; CONTENT_DB="$2"; '
                'podcast_already_published "$3" 2026-08-25',
                "bash",
                str(
                    Path(__file__).resolve().parents[1]
                    / "scripts"
                    / "daily_podcasts.sh"
                ),
                str(database),
                podcast_name,
            ],
            check=False,
        ).returncode

    assert skip_result("static_show") == 0
    assert skip_result("astro_show") == 0
    assert skip_result("wordpress_show") == 0
    assert skip_result("missing_artifact") == 1
    assert skip_result("not_published") == 1


def test_cron_is_night_only_and_cannot_truncate_active_log() -> None:
    """The installed source line runs once at night and only appends logs."""
    entry = CRON_ENTRY.read_text(encoding="utf-8").strip()
    assert entry.startswith("0 4 * * * ")
    assert ">>/tmp/daily_podcasts.log 2>&1" in entry
    assert " >/tmp/daily_podcasts.log" not in entry
