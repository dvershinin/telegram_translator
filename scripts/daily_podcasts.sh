#!/bin/bash
# Daily podcast pipeline: collect once, then run each podcast sequentially.
# A single podcast's failure must not starve the others. One logical episode
# date is captured at startup and retained even when generation crosses midnight.
set -o pipefail

PROJECT_DIR="/Users/danila/Projects/telegram_translator"
STATE_DIR="$HOME/Library/Application Support/telegram_translator"
SUCCESS_FILE="$STATE_DIR/daily-podcasts-success-date"
LOCK_FILE="$STATE_DIR/daily-podcasts.lock"
KEYCHAIN_FILE="$HOME/Library/Keychains/login.keychain-db"
CONTENT_DB="${CONTENT_DB:-$STATE_DIR/databases/content_store.db}"
SECURITY_BIN="${SECURITY_BIN:-/usr/bin/security}"
MCP_DEV="${MCP_DEV:-/Users/danila/.virtualenvs/mcps/bin/mcp-dev}"
CLI="python3 -m telegram_translator.cli"
VOICEBOX_URL="${VOICEBOX_URL:-http://localhost:17493}"
PIPELINE_FAILURES=""

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
        --arg "body=The scheduled podcast run for $run_date failed for: $PIPELINE_FAILURES. Inspect /tmp/daily_podcasts.log, then re-run that date after fixing the cause." \
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

run_podcast() {
    local name="$1"
    local run_date="$2"
    local wordpress_credentials=0

    if podcast_already_published "$name" "$run_date"; then
        echo "podcast $name already published for $run_date; skipping"
        return 0
    fi

    if [ "$name" = "scalable_stories" ]; then
        if ! GPS_WP_APP_PASSWORD="$("$SECURITY_BIN" find-generic-password \
                -a danila -s getpagespeed-scalable-stories-wordpress -w \
                "$KEYCHAIN_FILE")"; then
            echo "podcast $name failed: WordPress credential unavailable in Keychain"
            record_failure "$name (Keychain unavailable)"
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
    mkdir -p "$STATE_DIR"

    if [ "$(cat "$SUCCESS_FILE" 2>/dev/null)" = "$run_date" ]; then
        echo "daily podcast pipeline already succeeded for $run_date"
        return 0
    fi
    if ! /usr/bin/shlock -p "$$" -f "$LOCK_FILE"; then
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
