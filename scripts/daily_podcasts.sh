#!/bin/bash
# Daily podcast pipeline: collect once, then run each podcast sequentially.
# A single podcast's failure must not starve the others — voicebox or
# rsync hiccups in one show should not block the rest of the night.
set -eo pipefail

export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:/opt/homebrew/bin:$PATH"
source ~/.secrets
cd /Users/danila/Projects/telegram_translator

CLI="python3 -m telegram_translator.cli"
VOICEBOX_URL="${VOICEBOX_URL:-http://localhost:17493}"

# Collect sources (shared across all podcasts).
$CLI digest collect

# Pre-warm voicebox: launchd spawns it on first TCP hit; the FastAPI
# startup hook schedules TTS preload as a background task so /health
# returns within ~1 s of accept(). Bound the wait at 60 s so a truly
# dead backend doesn't hold the whole pipeline hostage.
if curl --max-time 60 --retry 15 --retry-delay 2 --retry-connrefused \
        -fsS "$VOICEBOX_URL/health" > /dev/null; then
    echo "voicebox ready at $VOICEBOX_URL"
else
    echo "voicebox pre-warm failed against $VOICEBOX_URL — pipeline will retry per podcast"
fi

run_podcast() {
    local name="$1"
    set +e
    $CLI digest summarize --podcast "$name" \
        && $CLI digest podcast --podcast "$name" \
        && $CLI digest publish --podcast "$name"
    local rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        echo "podcast $name failed (exit $rc); continuing with remaining podcasts"
    fi
    return 0
}

run_podcast crosswire
run_podcast the_stack
run_podcast vaske_daily
