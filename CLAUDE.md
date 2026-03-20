# Telegram Translator & Podcast Generator

## What This Project Does

Two independent systems sharing a codebase:

1. **Listener** (`telegram-translator start`) — Real-time Telegram channel monitor. Translates RU/UA messages to EN and forwards to output channels. Uses Telethon for Telegram, OpenAI/Idioma for translation.

2. **Digest Pipeline** (`telegram-translator digest run`) — Batch daily news aggregation. Collects from Telegram channels + RSS web feeds, generates LLM summaries, produces a podcast script, and synthesizes audio via Voicebox TTS.

## Architecture

```
telegram_translator/
  listener.py          — Real-time Telegram event handlers
  channel_manager.py   — Channel pair config (channels.yml)
  translation_manager.py — OpenAI/Idioma translation providers
  persistence_manager.py — Message dedup (listener only)
  digest.py            — Digest pipeline orchestrator
  content_store.py     — SQLite content index + digest records
  web_scraper.py       — RSS + trafilatura article extraction
  summarizer.py        — LLM summarization (OpenAI async)
  podcast_generator.py — Voicebox TTS + audio assembly
  config_manager.py    — YAML config + podcast resolution
  cli.py               — Click CLI (start, digest subcommands)
```

## Multi-Podcast System

The digest pipeline supports multiple named podcasts. Each podcast has its own:
- Identity: `title`, `host_name` (template vars in prompts: `{title}`, `{host_name}`, `{date}`)
- Sources: references global source pool by name
- Voice profile: Voicebox voice to use
- Audio assets: intro bed, background bed, whoosh (file paths)
- Audio mixing params: volumes, fade durations, lead-in
- Prompts: `executive_prompt`, `podcast_prompt`, `selection_prompt`

Config lives in `config.yml` under `podcasts:` section. Legacy flat `digest:`+`podcast:` sections still work (synthesized as `_default` podcast).

The `digests` table is keyed by `(date, podcast_name)`.

## Key Commands

```bash
telegram-translator start                              # Run listener
telegram-translator digest run [--podcast NAME]        # Full pipeline
telegram-translator digest collect                     # Fetch sources only
telegram-translator digest summarize --podcast NAME    # Summarize only
telegram-translator digest podcast --podcast NAME      # Generate audio only
telegram-translator digest podcasts                    # List configured podcasts
telegram-translator digest status --date 2026-03-20   # Show digest status
```

## Voicebox Integration

- API at `http://localhost:17493` (configurable per-podcast via `voicebox.url`)
- POST `/generate` returns JSON with `id` and `duration` (not raw audio)
- GET `/audio/{id}` returns the WAV file
- TTS is slow on Apple Silicon MLX (~8 min per 500-char segment)
- Scripts split at 500-char sentence boundaries, ~13 segments for a 6-min podcast
- Total generation time: ~90 minutes for a full podcast
- Backend: `cd ~/Projects/voicebox && backend/venv/bin/uvicorn backend.main:app --port 17493`

## Audio Assembly

`PodcastGenerator.assemble_podcast()` mixes three layers:
1. **Voice** — TTS segments concatenated with whoosh transitions at topic boundaries (detected by `**` markdown headers), short silence between sub-segments
2. **Intro bed** — Plays solo during configurable lead-in, then fades out under voice
3. **Background bed** — Loops with crossfade, plays at low volume throughout, fades in/out

All audio params (asset paths, volumes, fades) are configurable per-podcast under `audio:` in config.

Audio assets live in `podcasts/assets/` (WAV/MP3, converted to mono 24kHz internally).

## Database

SQLite at `~/Library/Application Support/telegram_translator/databases/`:
- `persistence.db` — Listener message dedup
- `content_store.db` — Digest content items + digest records

The `content_items` table is shared (podcast-agnostic). The `digests` table has `UNIQUE(date, podcast_name)`.

Auto-migration: old `digests` tables without `podcast_name` are migrated on startup.

## Config Files

- `config.yml` — Main config (API keys, sources, podcasts). **Contains secrets — not committed.**
- `config.yml.example` — Template with structure documentation
- `channels.yml` — Listener channel pairs (separate from digest sources)

## Dependencies

Core: `telethon`, `openai`, `click`, `pyyaml`, `appdirs`
Digest: `feedparser`, `trafilatura`, `httpx`, `pydub`

## Development Notes

- Python 3.12+, no virtualenv (system Python)
- Telethon session at `~/Library/Application Support/telegram_translator/sessions/`
- First Telegram auth is interactive — run `telegram-translator start` once manually
- `digest collect` uses `client.connect()` + `is_user_authorized()` (no interactive prompt)
- OPENAI_API_KEY must be set (via `source ~/.secrets`) for summarization
