#!/bin/bash
# Daily podcast pipeline: collect once, then run each podcast sequentially.
# A single podcast's failure must not starve the others. The hourly cron retry
# is bounded by a per-day success marker and this process lock.
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
    local today
    today="$(date +%Y-%m-%d)"
    if [ ! -x "$MCP_DEV" ]; then
        echo "podcast failure alert unavailable: $MCP_DEV is not executable" >&2
        return 1
    fi
    "$MCP_DEV" call system human_action_alert \
        --arg "title=Daily podcast pipeline failed" \
        --arg "body=The scheduled podcast run failed for: $PIPELINE_FAILURES. Inspect /tmp/daily_podcasts.log. Cron will retry hourly through 23:00 until one full run succeeds." \
        --arg "urgency=attention" \
        --arg "dedupe_key=telegram-translator-daily-podcasts-$today" \
        --arg "cooldown_seconds=82800" \
        --arg "working_directory=$PROJECT_DIR"
}

podcast_already_published() {
    local name="$1"
    local today="$2"
    python3 -c '
import sqlite3
import sys

database, date, podcast = sys.argv[1:]
try:
    row = sqlite3.connect(database).execute(
        "SELECT status FROM digests WHERE date = ? AND podcast_name = ?",
        (date, podcast),
    ).fetchone()
except sqlite3.Error:
    raise SystemExit(1)
raise SystemExit(0 if row and row[0] == "published" else 1)
' "$CONTENT_DB" "$today" "$name"
}

run_podcast() {
    local name="$1"
    local today
    local wordpress_credentials=0
    today="$(date +%Y-%m-%d)"

    if podcast_already_published "$name" "$today"; then
        echo "podcast $name already published for $today; skipping"
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

    $CLI digest summarize --podcast "$name" \
        && $CLI digest podcast --podcast "$name" \
        && $CLI digest publish --podcast "$name"
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
    local today
    today="$(date +%Y-%m-%d)"
    mkdir -p "$STATE_DIR"

    if [ "$(cat "$SUCCESS_FILE" 2>/dev/null)" = "$today" ]; then
        echo "daily podcast pipeline already succeeded for $today"
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
        alert_failures || true
        return 1
    fi
    if ! cd "$PROJECT_DIR"; then
        record_failure "project directory"
        alert_failures || true
        return 1
    fi

    # Collect sources once for the shared daily run.
    if ! $CLI digest collect; then
        record_failure "collection"
        alert_failures || true
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

    run_podcast crosswire
    run_podcast the_stack
    run_podcast scalable_stories
    run_podcast vaske_daily

    if [ -n "$PIPELINE_FAILURES" ]; then
        alert_failures || true
        return 1
    fi

    printf '%s\n' "$today" > "$SUCCESS_FILE.$$"
    mv "$SUCCESS_FILE.$$" "$SUCCESS_FILE"
    echo "daily podcast pipeline succeeded for $today"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
