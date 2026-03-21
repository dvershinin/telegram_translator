#!/bin/bash
# Daily podcast pipeline: collect once, then run each podcast sequentially.
set -eo pipefail

export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:/opt/homebrew/bin:$PATH"
source ~/.secrets
cd /Users/danila/Projects/telegram_translator

CLI="python3 -m telegram_translator.cli"

# Collect sources (shared across all podcasts)
$CLI digest collect

# Crosswire: summarize → generate audio → publish
$CLI digest summarize --podcast crosswire
$CLI digest podcast --podcast crosswire
$CLI digest publish --podcast crosswire

# The Stack: summarize → generate audio → publish
$CLI digest summarize --podcast the_stack
$CLI digest podcast --podcast the_stack
$CLI digest publish --podcast the_stack
