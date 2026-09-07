"""Structural guards for the unattended daily podcast runner."""

import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "daily_podcasts.sh"
).read_text(encoding="utf-8")
CRON_ENTRY = Path(__file__).resolve().parents[1] / "scripts" / "daily_podcasts.cron"
CRON_INSTALLER = (
    Path(__file__).resolve().parents[1] / "scripts" / "install_daily_podcasts_cron.sh"
)


def test_scalable_stories_runs_before_russian_podcast() -> None:
    """The English WordPress show should run before the Russian pipeline."""
    assert SCRIPT.index("run_podcast scalable_stories") < SCRIPT.index(
        "run_podcast vaske_daily"
    )


def test_wordpress_credentials_are_scoped_to_scalable_stories() -> None:
    """Load only the show's password without requiring the login Keychain."""
    assert 'if [ "$name" = "scalable_stories" ]' in SCRIPT
    assert "find-generic-password" not in SCRIPT
    assert '"$WORDPRESS_PASSWORD_FILE"' in SCRIPT
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


@pytest.mark.parametrize("stage_failure", [False, True])
def test_private_password_works_without_login_context(
    tmp_path: Path, stage_failure: bool
) -> None:
    """Load from a private file and clear secrets even when a stage fails."""
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir(mode=0o700)
    password_file = credential_dir / "scalable-stories-wordpress-password"
    password_file.write_text("test application password\n", encoding="utf-8")
    password_file.chmod(0o600)
    shell = r"""
source "$1"
WORDPRESS_PASSWORD_FILE="$2"
podcast_already_published() { return 1; }
mock_cli() {
    if [ "${GPS_WP_USER:-}" != danila ] ||
       [ "${GPS_WP_APP_PASSWORD:-}" != 'test application password' ]; then
        echo wrong-credentials
        return 90
    fi
    echo "stage $2"
    [ "$3" = --date ] || return 91
    [ "$STAGE_FAILURE" = false ]
}
CLI=mock_cli
run_podcast scalable_stories 2026-09-08
[ "${GPS_WP_USER+x}${GPS_WP_APP_PASSWORD+x}" = '' ] || exit 92
printf 'failures:%s\n' "$PIPELINE_FAILURES"
mock_cli() {
    [ "${GPS_WP_USER+x}${GPS_WP_APP_PASSWORD+x}" = '' ] || exit 93
}
run_podcast vaske_daily 2026-09-08
"""
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            shell,
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(password_file),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "STAGE_FAILURE": str(stage_failure).lower(),
        },
    )
    assert "wrong-credentials" not in result.stdout
    assert "test application password" not in result.stdout + result.stderr
    assert "stage summarize" in result.stdout
    assert ("stage publish" in result.stdout) is not stage_failure
    assert ("scalable_stories (exit 1)" in result.stdout) is stage_failure


@pytest.mark.parametrize(
    "bad_file",
    [
        "missing",
        "empty",
        "public",
        "symlink",
        "public_dir",
        "multiline",
        "trailing_blank",
    ],
)
def test_unsafe_password_is_rejected_without_running_stages(
    tmp_path: Path, bad_file: str
) -> None:
    """Reject unavailable or unsafe files and discard inherited credentials."""
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir(mode=0o700)
    password_file = credential_dir / "scalable-stories-wordpress-password"
    password_file.write_text("test-secret\n", encoding="utf-8")
    password_file.chmod(0o600)
    if bad_file == "missing":
        password_file.unlink()
    elif bad_file == "empty":
        password_file.write_text("\n", encoding="utf-8")
    elif bad_file == "public":
        password_file.chmod(0o644)
    elif bad_file == "symlink":
        target = credential_dir / "target"
        password_file.rename(target)
        password_file.symlink_to(target)
    elif bad_file == "public_dir":
        credential_dir.chmod(0o755)
    elif bad_file == "multiline":
        password_file.write_text("test-secret\nextra-line\n", encoding="utf-8")
    elif bad_file == "trailing_blank":
        password_file.write_text("test-secret\n\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; WORDPRESS_PASSWORD_FILE="$2"; '
            "podcast_already_published() { return 1; }; "
            "mock_cli() { echo unexpected-cli; }; CLI=mock_cli; "
            "run_podcast scalable_stories 2026-09-08; "
            '[ "${GPS_WP_USER+x}${GPS_WP_APP_PASSWORD+x}" = "" ] || exit 92; '
            'printf "%s" "$PIPELINE_FAILURES"',
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(password_file),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GPS_WP_USER": "stale-user",
            "GPS_WP_APP_PASSWORD": "stale-secret",
        },
    )
    assert "scalable_stories (credential unavailable)" in result.stdout
    assert "unexpected-cli" not in result.stdout
    assert "test-secret" not in result.stdout + result.stderr
    assert "stale-secret" not in result.stdout + result.stderr


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
    assert "daily_podcasts_2026-08-25.log" in args
    assert "/tmp/" not in args
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
            "podcast_already_published scalable_stories 2026-08-24",
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
            "podcast_already_published crosswire 2026-08-24",
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(database),
        ],
        check=False,
    )
    assert missing.returncode == 1


@pytest.mark.parametrize("credential_available", [True, False])
def test_main_pins_one_logical_date_across_midnight(
    tmp_path: Path, credential_available: bool
) -> None:
    """Pin dates and advance the marker only when every show succeeds."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".secrets").write_text("", encoding="utf-8")
    credential_dir = (
        fake_home / "Library/Application Support/telegram_translator/credentials"
    )
    credential_dir.mkdir(parents=True, mode=0o700)
    password_file = credential_dir / "scalable-stories-wordpress-password"
    password_file.write_text("test-password\n", encoding="utf-8")
    password_file.chmod(0o600)
    if not credential_available:
        password_file.unlink()
    capture = tmp_path / "calls"
    date_called = tmp_path / "date-called"
    state_dir = tmp_path / "state"

    shell = r"""
source "$1"
STATE_DIR="$2"
SUCCESS_FILE="$STATE_DIR/success"
LOCK_FILE="$STATE_DIR/lock"
CONTENT_DB="$STATE_DIR/content.sqlite"
WORDPRESS_PASSWORD_FILE="$3"
CAPTURE="$4"
DATE_CALLED="$5"
PROJECT_DIR="$6"
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
alert_failures() { printf 'alert %s %s\n' "$1" "$PIPELINE_FAILURES" >> "$CAPTURE"; }
CLI=mock_cli
main
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            shell,
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(state_dir),
            str(password_file),
            str(capture),
            str(date_called),
            str(Path(__file__).resolve().parents[1]),
        ],
        check=False,
        env=os.environ | {"HOME": str(fake_home)},
    )

    calls = capture.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "cli digest collect --date 2026-08-25"
    assert all("2026-08-25" in call for call in calls)
    assert not any("2026-08-26" in call for call in calls)
    assert any("digest publish" in call and "vaske_daily" in call for call in calls)
    if credential_available:
        assert result.returncode == 0
        assert (state_dir / "success").read_text(encoding="utf-8") == "2026-08-25\n"
    else:
        assert result.returncode == 1
        assert not (state_dir / "success").exists()
        assert calls[-1] == "alert 2026-08-25 scalable_stories (credential unavailable)"


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
    # The fallback log must survive reboot: /tmp is wiped, which destroyed
    # the 2026-09-07 failure evidence.
    assert "/tmp/" not in entry
    assert 'mkdir -p "$HOME/Library/Logs/telegram_translator"' in entry
    assert '>>"$HOME/Library/Logs/telegram_translator/daily_podcasts.log" 2>&1' in entry


def test_runner_logs_to_durable_dated_file_with_retention(tmp_path: Path) -> None:
    """setup_logging appends to a dated reboot-surviving log and prunes old ones."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "daily_podcasts_2020-01-01.log"
    stale.write_text("old\n", encoding="utf-8")
    os.utime(stale, (0, 0))
    fresh = log_dir / "daily_podcasts_2026-09-06.log"
    fresh.write_text("recent\n", encoding="utf-8")

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; LOG_DIR="$2"; '
            'setup_logging 2026-09-07; echo "inside the log"',
            "bash",
            str(Path(__file__).resolve().parents[1] / "scripts/daily_podcasts.sh"),
            str(log_dir),
        ],
        check=True,
    )

    dated = log_dir / "daily_podcasts_2026-09-07.log"
    content = dated.read_text(encoding="utf-8")
    assert "daily podcast run started" in content
    assert "inside the log" in content
    assert not stale.exists()
    assert fresh.exists()


def test_cron_installer_replaces_only_podcast_entry(tmp_path: Path) -> None:
    """The privileged installer preserves every unrelated cron entry."""
    cron_store = tmp_path / "crontab"
    cron_store.write_text(
        "15 1 * * * /usr/local/bin/unrelated\n"
        "0 4-23 * * * "
        "/Users/danila/Projects/telegram_translator/"
        "scripts/daily_podcasts.sh >/tmp/daily_podcasts.log 2>&1\n",
        encoding="utf-8",
    )
    fake_crontab = tmp_path / "crontab-bin"
    fake_crontab.write_text(
        "#!/bin/bash\n"
        'if [ "$3" = "-l" ]; then cat "$CRON_STORE"; '
        'else cp "$3" "$CRON_STORE"; fi\n',
        encoding="utf-8",
    )
    fake_crontab.chmod(0o755)
    fake_sudo = tmp_path / "sudo-bin"
    fake_sudo.write_text(
        "#!/bin/bash\n" 'if [ "$1" = "-v" ]; then exit 0; fi\n' 'exec "$@"\n',
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    subprocess.run(
        ["bash", str(CRON_INSTALLER)],
        check=True,
        env=os.environ
        | {
            "CRON_STORE": str(cron_store),
            "CRONTAB_BIN": str(fake_crontab),
            "SUDO_BIN": str(fake_sudo),
            "TARGET_USER": "danila",
            "RUNNER_PATH": (
                "/Users/danila/Projects/telegram_translator/"
                "scripts/daily_podcasts.sh"
            ),
        },
    )

    lines = cron_store.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "15 1 * * * /usr/local/bin/unrelated"
    assert lines[1] == CRON_ENTRY.read_text(encoding="utf-8").strip()
    assert len(lines) == 2
