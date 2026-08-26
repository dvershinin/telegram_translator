#!/bin/bash
# Install the versioned daily podcast cron entry for the invoking user.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_PATH="$SCRIPT_DIR/daily_podcasts.sh"
ENTRY_FILE="$SCRIPT_DIR/daily_podcasts.cron"
TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(id -un)}}"
SUDO_BIN="${SUDO_BIN:-/usr/bin/sudo}"
CRONTAB_BIN="${CRONTAB_BIN:-/usr/bin/crontab}"

current_file="$(mktemp)"
candidate_file="$(mktemp)"
verified_file="$(mktemp)"
trap 'rm -f "$current_file" "$candidate_file" "$verified_file"' EXIT

"$SUDO_BIN" -v
if ! "$SUDO_BIN" "$CRONTAB_BIN" -u "$TARGET_USER" -l > "$current_file" 2>/dev/null; then
    : > "$current_file"
fi

awk -v runner="$RUNNER_PATH" 'index($0, runner) == 0' \
    "$current_file" > "$candidate_file"
cat "$ENTRY_FILE" >> "$candidate_file"

"$SUDO_BIN" "$CRONTAB_BIN" -u "$TARGET_USER" "$candidate_file"
"$SUDO_BIN" "$CRONTAB_BIN" -u "$TARGET_USER" -l > "$verified_file"

expected="$(cat "$ENTRY_FILE")"
if [ "$(grep -Fxc "$expected" "$verified_file")" -ne 1 ] \
        || [ "$(grep -Fc "$RUNNER_PATH" "$verified_file")" -ne 1 ]; then
    echo "podcast cron verification failed" >&2
    exit 1
fi

echo "Installed podcast cron for $TARGET_USER: $expected"
