#!/bin/bash
# Daily podcast pipeline: collect once, then run each podcast sequentially.
# A single podcast's failure must not starve the others. One logical episode
# date is captured at startup and retained even when generation crosses midnight.
set -o pipefail

PROJECT_DIR="/Users/danila/Projects/telegram_translator"
STATE_DIR="$HOME/Library/Application Support/telegram_translator"
SUCCESS_FILE="$STATE_DIR/daily-podcasts-success-date"
LOCK_FILE="$STATE_DIR/daily-podcasts.lock"
WORDPRESS_PASSWORD_FILE="$STATE_DIR/credentials/scalable-stories-wordpress-password"
CONTENT_DB="${CONTENT_DB:-$STATE_DIR/databases/content_store.db}"
SHLOCK_BIN="${SHLOCK_BIN:-/usr/bin/shlock}"
MCP_DEV="${MCP_DEV:-/Users/danila/.virtualenvs/mcps/bin/mcp-dev}"
CLI="python3 -m telegram_translator.cli"
VOICEBOX_URL="${VOICEBOX_URL:-http://localhost:17493}"
LOG_DIR="${LOG_DIR:-$HOME/Library/Logs/telegram_translator}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-60}"
PIPELINE_FAILURES=""

setup_logging() {
    # Redirect all further output to a reboot-surviving dated log. /tmp is
    # wiped on reboot, which made the 2026-09-07 crosswire failure unpinnable.
    local run_date="$1"
    mkdir -p "$LOG_DIR" || return 0
    exec >> "$LOG_DIR/daily_podcasts_$run_date.log" 2>&1
    echo "=== daily podcast run started $(date '+%Y-%m-%dT%H:%M:%S%z') (episode date $run_date) ==="
    find "$LOG_DIR" -name 'daily_podcasts_*.log' \
        -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
}

record_failure() {
    local name="$1"
    if [ -n "$PIPELINE_FAILURES" ]; then
        PIPELINE_FAILURES="$PIPELINE_FAILURES, $name"
    else
        PIPELINE_FAILURES="$name"
    fi
}

alert_failures() {
    local run_date="$1"
    if [ ! -x "$MCP_DEV" ]; then
        echo "podcast failure alert unavailable: $MCP_DEV is not executable" >&2
        return 1
    fi
    "$MCP_DEV" call system human_action_alert \
        --arg "title=Daily podcast pipeline failed" \
        --arg "body=The scheduled podcast run for $run_date failed for: $PIPELINE_FAILURES. Inspect $LOG_DIR/daily_podcasts_$run_date.log, then re-run that date after fixing the cause." \
        --arg "urgency=attention" \
        --arg "dedupe_key=telegram-translator-daily-podcasts-$run_date" \
        --arg "cooldown_seconds=82800" \
        --arg "working_directory=$PROJECT_DIR"
}

podcast_already_published() {
    local name="$1"
    local run_date="$2"
    python3 -c '
import sqlite3
import sys
from pathlib import Path

database, date, podcast, project_dir = sys.argv[1:]
try:
    row = sqlite3.connect(database).execute(
        "SELECT published_at, m4a_path FROM digests "
        "WHERE date = ? AND podcast_name = ?",
        (date, podcast),
    ).fetchone()
except sqlite3.Error:
    raise SystemExit(1)
if not row or not row[0] or not row[1]:
    raise SystemExit(1)
artifact = Path(row[1])
if not artifact.is_absolute():
    artifact = Path(project_dir) / artifact
raise SystemExit(0 if artifact.is_file() else 1)
' "$CONTENT_DB" "$run_date" "$name" "$PROJECT_DIR"
}

read_wordpress_password() {
    # Read data, never source shell code. Fail closed before emitting any secret.
    python3 - "$WORDPRESS_PASSWORD_FILE" <<'PY'
import os
from pathlib import Path
import stat
import sys

try:
    path = Path(sys.argv[1])
    directory = path.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700):
        raise ValueError("unsafe credential directory")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as credential:
        metadata = os.fstat(credential.fileno())
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1):
            raise ValueError("unsafe credential file")
        raw = credential.read(4097)
    password = raw.decode("utf-8").removesuffix("\n")
    if (len(raw) > 4096 or not password.strip()
            or any(char in password for char in ("\n", "\r", "\0"))):
        raise ValueError("invalid credential contents")
except (OSError, ValueError):
    print("WordPress credential file unavailable or unsafe", file=sys.stderr)
    raise SystemExit(1)
sys.stdout.write(password)
PY
}

run_podcast() {
    local name="$1"
    local run_date="$2"
    local wordpress_credentials=0
    unset GPS_WP_USER GPS_WP_APP_PASSWORD

    if podcast_already_published "$name" "$run_date"; then
        echo "podcast $name already published for $run_date; skipping"
        return 0
    fi

    if [ "$name" = "scalable_stories" ]; then
        if ! GPS_WP_APP_PASSWORD="$(read_wordpress_password)"; then
            unset GPS_WP_USER GPS_WP_APP_PASSWORD
            echo "podcast $name failed: WordPress credential file unavailable or unsafe"
            record_failure "$name (credential unavailable)"
            return 0
        fi
        export GPS_WP_USER=danila
        export GPS_WP_APP_PASSWORD
        wordpress_credentials=1
    fi

    $CLI digest summarize --date "$run_date" --podcast "$name" \
        && $CLI digest podcast --date "$run_date" --podcast "$name" \
        && $CLI digest publish --date "$run_date" --podcast "$name"
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "podcast $name failed (exit $rc); continuing with remaining podcasts"
        record_failure "$name (exit $rc)"
    fi
    if [ "$wordpress_credentials" -eq 1 ]; then
        unset GPS_WP_USER GPS_WP_APP_PASSWORD
    fi
    return 0
}

main() {
    local run_date
    run_date="$(date +%Y-%m-%d)"
    setup_logging "$run_date"
    mkdir -p "$STATE_DIR"

    if [ "$(cat "$SUCCESS_FILE" 2>/dev/null)" = "$run_date" ]; then
        echo "daily podcast pipeline already succeeded for $run_date"
        return 0
    fi
    if ! "$SHLOCK_BIN" -p "$$" -f "$LOCK_FILE"; then
        echo "daily podcast pipeline already running"
        return 0
    fi
    trap 'rm -f "$LOCK_FILE"' EXIT

    export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:/opt/homebrew/bin:$PATH"
    # shellcheck source=/dev/null
    if ! source "$HOME/.secrets"; then
        record_failure "environment setup"
        alert_failures "$run_date" || true
        return 1
    fi
    if ! cd "$PROJECT_DIR"; then
        record_failure "project directory"
        alert_failures "$run_date" || true
        return 1
    fi

    # Collect sources once for the shared daily run.
    if ! $CLI digest collect --date "$run_date"; then
        record_failure "collection"
        alert_failures "$run_date" || true
        return 1
    fi

    # Pre-warm voicebox: launchd spawns it on first TCP hit. The pipeline
    # commands retain their own retry/error handling if this bounded probe fails.
    if curl --max-time 60 --retry 15 --retry-delay 2 --retry-connrefused \
            -fsS "$VOICEBOX_URL/health" > /dev/null; then
        echo "voicebox ready at $VOICEBOX_URL"
    else
        echo "voicebox pre-warm failed against $VOICEBOX_URL; pipeline will retry per podcast"
    fi

    run_podcast crosswire "$run_date"
    run_podcast the_stack "$run_date"
    run_podcast scalable_stories "$run_date"
    run_podcast vaske_daily "$run_date"

    if [ -n "$PIPELINE_FAILURES" ]; then
        alert_failures "$run_date" || true
        return 1
    fi

    printf '%s\n' "$run_date" > "$SUCCESS_FILE.$$"
    mv "$SUCCESS_FILE.$$" "$SUCCESS_FILE"
    echo "daily podcast pipeline succeeded for $run_date"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
