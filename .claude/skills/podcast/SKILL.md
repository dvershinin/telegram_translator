---
name: podcast
description: Run the daily podcast pipeline — collect, summarize, generate audio, publish, and deploy. Use when asked to produce a podcast episode.
argument-hint: "[podcast-name] [--step collect|summarize|podcast|publish|all]"
disable-model-invocation: true
allowed-tools: Bash, Read, Grep, Glob, Agent
---

# Podcast Pipeline

Run the full podcast production pipeline for a daily episode. Default podcast: `crosswire`. Default step: `all`.

## Arguments

- `$ARGUMENTS[0]` — podcast name (default: `crosswire`)
- `--step <name>` — run only a specific step: `collect`, `summarize`, `podcast`, `publish`, or `all`
- `--no-cache` — bypass LLM and TTS caches for this run

## Prerequisites

Before generating audio (`podcast` or `all` steps), verify Voicebox is running:

```bash
curl -sf http://localhost:17493/profiles > /dev/null && echo "Voicebox: OK" || echo "Voicebox: NOT RUNNING"
```

If not running, tell the user to start it:
```
cd ~/Projects/voicebox && backend/venv/bin/uvicorn backend.main:app --port 17493
```
Do NOT start Voicebox yourself — it runs in a separate terminal.

## Environment

Always source secrets before running any pipeline command:
```bash
source ~/.secrets
```

The CLI entry point is:
```bash
python3 -m telegram_translator.cli
```

## Pipeline Steps

### Step 1: Collect (`collect`)
Fetch content from all configured sources (Telegram + RSS feeds).
```bash
python3 -m telegram_translator.cli digest collect
```
This is shared across all podcasts — run once per day.

### Step 2: Summarize (`summarize`)
Run content selection, per-source summarization, executive summary, and podcast script generation.
```bash
python3 -m telegram_translator.cli digest summarize --podcast <name>
```
Add `--no-cache` to force regeneration.

After this step, verify the script is valid structured JSON with sections (needed for whoosh transitions):
```bash
python3 -c "import json,sqlite3,pathlib; db=pathlib.Path.home()/'Library/Application Support/telegram_translator/databases/content_store.db'; r=sqlite3.connect(db).execute('SELECT podcast_script FROM digests WHERE date=? AND podcast_name=?',('$(date +%Y-%m-%d)','<name>')).fetchone(); d=json.loads(r[0]); topics=[s['topic'] for s in d['sections'] if s.get('topic')]; print(f'{len(d[\"sections\"])} sections, topics: {topics}')"
```
If parsing fails, the LLM returned plain text instead of JSON — consider clearing cache and retrying.

### Step 3: Generate Audio (`podcast`)
Synthesize the podcast script into audio via Voicebox TTS. This takes ~90 minutes.
```bash
python3 -m telegram_translator.cli digest podcast --podcast <name>
```
Run this in the background since it's long-running. Check the log output for segment count and topic boundary count.

### Step 4: Publish (`publish`)
Encode WAV → M4A, rebuild RSS feed (with validation), generate HTML index page, and deploy via rsync.
```bash
python3 -m telegram_translator.cli digest publish --podcast <name>
```

### Full pipeline (`all`)
Runs steps 1–3 in sequence (collect + summarize + podcast):
```bash
python3 -m telegram_translator.cli digest run --podcast <name>
```
Then publish separately:
```bash
python3 -m telegram_translator.cli digest publish --podcast <name>
```

## Post-publish verification

After publishing, verify the feed:
```bash
curl -sf https://podcasts.getpagespeed.com/<name>/feed.xml | head -20
```

Check CDATA is present (should be 2 per episode):
```bash
curl -sf https://podcasts.getpagespeed.com/<name>/feed.xml | grep -c 'CDATA'
```

## Clearing caches

If you need to regenerate from scratch:
```bash
python3 -m telegram_translator.cli digest cache clear
```
This clears both LLM cache (SQLite) and TTS segment cache (WAV files).

## Available podcasts

List all configured podcasts:
```bash
python3 -m telegram_translator.cli digest podcasts
```

Current podcasts: `crosswire` (geopolitics/tech/science), `the_stack` (tech-only).
