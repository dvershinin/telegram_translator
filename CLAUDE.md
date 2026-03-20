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
  content_store.py     — SQLite content index + digest records + LLM cache
  web_scraper.py       — RSS + trafilatura article extraction
  summarizer.py        — LLM summarization (OpenAI async, cached)
  podcast_generator.py — Voicebox TTS + audio assembly (segment cache)
  audio_encoder.py     — WAV → M4A (AAC) encoding via pydub/ffmpeg
  feed_generator.py    — RSS 2.0 + iTunes namespace feed generation
  publisher.py         — Encode, build feed, deploy (rsync)
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
telegram-translator digest publish --podcast NAME      # Encode M4A + build feed + deploy
telegram-translator digest feed --podcast NAME         # Rebuild RSS feed only
telegram-translator digest cache clear                 # Clear LLM + TTS caches
telegram-translator digest podcasts                    # List configured podcasts
telegram-translator digest status --date 2026-03-20   # Show digest status
```

Use `/podcast [name]` skill to run the full pipeline interactively — it handles secrets, Voicebox checks, all steps, and verification. See `.claude/skills/podcast/SKILL.md`.

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

## Caching

Two caching layers reduce cost and time on reruns:

- **LLM cache** — `llm_cache` table in `content_store.db`. Keyed by `"{stage}:{podcast}:{SHA256(system+user+model)}"`. Cache hits skip the OpenAI API call entirely. Stages: `selection`, `source_summary`, `executive`, `script`.
- **TTS segment cache** — WAV files at `.cache/tts/` in the project root (gitignored). Keyed by `SHA256(text + voice_profile)`. Cache hits skip the Voicebox API call (~8 min per segment).

Flags: `--no-cache` on `digest summarize`, `digest podcast`, `digest run` bypasses cache for one run. `digest cache clear` deletes all cache entries.

## Episode Continuity

Before generating the executive summary, the pipeline fetches up to 3 recent prior episode summaries and injects them into the LLM prompt with the instruction: "focus on what is NEW today, don't re-explain these stories from scratch." This prevents multi-day stories from being re-explained verbatim each episode.

## Publishing Pipeline

`digest publish` encodes WAV → M4A (AAC), builds an RSS 2.0 + iTunes feed, generates an HTML index page, and optionally deploys via rsync.

Per-podcast config under `publish:` keys: `base_url`, `publish_dir`, `m4a_bitrate`, `show_artwork`, `show_description`, `show_category`, `show_subcategory`, `explicit`, `copyright`, `sync_command`.

Publish directory structure:
```
publish/{podcast_name}/
  feed.xml
  index.html
  artwork.jpg
  episodes/
    {podcast_name}_{date}.m4a
```

See `docs/publishing.md` for the full guide including Apple Podcasts submission.

## RSS Feed — Apple Podcasts Compliance

The feed generator (`feed_generator.py`) targets Apple Podcasts spec compliance. Key design decisions:

**Namespaces used** (only these three — validated against Apple spec):
- `itunes:` — Apple's own podcast namespace (required)
- `content:` — for `<content:encoded>` HTML show notes (Apple-supported)
- `atom:` — for `<atom:link rel="self">` canonical URL (PSP-1 best practice, harmless)

**Namespaces NOT used** (intentionally excluded):
- `podcast:` (podcastindex.org) — Apple ignores entirely. Tags like `podcast:locked`, `podcast:medium` are Podcasting 2.0 only.

**Deprecated tags NOT used**:
- `itunes:owner` — deprecated by Apple since Aug 2022, they use Apple ID from Podcasts Connect instead
- `itunes:summary`, `itunes:subtitle`, `itunes:keywords` — all deprecated

**CDATA for HTML descriptions** (per Apple spec):
- Episode `<description>` and `<content:encoded>` contain HTML wrapped in `<![CDATA[...]]>`
- Show-level `<description>` is plain text (from config)
- ElementTree can't produce CDATA natively — a marker/post-processing approach is used (`_CDATA_MARK` prefix → `_inject_cdata()` after serialization)

**Built-in validation**:
- `_validate_feed()` runs before every write, checks all Apple-required tags at show and episode level
- Raises `ValueError` with details if anything is missing (e.g., enclosure without url/length/type)

**HTML index page**:
- `publisher.py` generates `index.html` alongside `feed.xml` on every rebuild
- Dark theme, mobile-friendly, inline audio player, collapsible show notes
- Episode summaries converted from Markdown to HTML via `_markdown_to_html()`

## Tests

```bash
python3 -m pytest tests/ -v
```

- `tests/test_feed_generator.py` — 43 tests covering Apple-required/recommended tags, CDATA wrapping, Markdown-to-HTML conversion, episode numbering, validation

## Database

SQLite at `~/Library/Application Support/telegram_translator/databases/`:
- `persistence.db` — Listener message dedup
- `content_store.db` — Digest content items + digest records + LLM cache

The `content_items` table is shared (podcast-agnostic). The `digests` table has `UNIQUE(date, podcast_name)` and publish columns (`m4a_path`, `duration_seconds`, `published_at`). The `llm_cache` table is keyed by `cache_key`.

Auto-migration: old `digests` tables without `podcast_name` or publish columns are migrated on startup.

## Config Files

- `config.yml` — Main config (API keys, sources, podcasts). **Contains secrets — not committed.**
- `config.yml.example` — Template with structure documentation
- `channels.yml` — Listener channel pairs (separate from digest sources)

## Dependencies

Core: `telethon`, `openai`, `click`, `pyyaml`, `appdirs`
Digest: `feedparser`, `trafilatura`, `httpx`, `pydub`
Publishing: `mutagen` (M4A metadata tagging)

## Telegram Sessions

This project uses its **own dedicated Telethon session**, separate from `~/Projects/tgp`. Telegram allows multiple sessions per account (like multiple devices), so both projects can coexist.

- Session file: `~/Library/Application Support/telegram_translator/sessions/telegram_translator_session.session`
- API credentials: `TTR_API_ID` / `TTR_API_HASH` in `~/.secrets` (falls back to `api_id`/`api_hash` in `config.yml`)
- **First auth is interactive**: run `source ~/.secrets && telegram-translator start`, enter phone code, then Ctrl+C once the bot starts.
- After that, `digest collect` connects non-interactively via `client.connect()` + `is_user_authorized()`.
- If session expires (Telegram revokes it), re-run `telegram-translator start` to re-authenticate.
- Do NOT share session files between projects — Telethon locks the SQLite file and concurrent access causes disconnections.

## LLM Prompt Guardrails

The summarizer injects guardrails into both the executive summary and podcast script prompts:

- **Executive stage**: "Only cover topics that have actual content. Do NOT invent categories or mention that a topic had no developments."
- **Script stage**: "Never mention that a topic had no news or was quiet. Only discuss topics present in the summary. Mark each major topic transition with a line starting with `**Topic Name**` (Markdown bold)."

These prevent the LLM from generating filler about empty categories (e.g., "Science was quiet today..."), regardless of what the user-configured prompt says.

The `**Topic Name**` headers in the script are detected by `split_script_by_topics()` in `podcast_generator.py` to insert whoosh sound effects at topic transitions.

## Script Output

During TTS generation, the podcast script is written to `{output_dir}/{podcast_name}_{date}.txt` before audio synthesis starts, so it can be reviewed while Voicebox is running.

## Development Notes

- Python 3.12+, no virtualenv (system Python)
- Secrets: `source ~/.secrets` — provides `OPENAI_API_KEY`, `TTR_API_ID`, `TTR_API_HASH`, `TGP_API_ID`, `TGP_API_HASH`
- Related project: `~/Projects/tgp` — Telegram profile manager with its own sessions (do not share)
- Hosting: `podcasts.getpagespeed.com` on Linode, nginx vhost in `~/Projects/ansible/host_vars/web.getpagespeed.com.yml`
- Nginx config: `static` template with `expires 5m` override for `.xml` files (feed freshness), `dynamic_extensions: [xml]`
- Repo: private at `git@github.com:dvershinin/telegram_translator.git`, no upstream fork
- CLI entry point: `python3 -m telegram_translator.cli` (or `telegram-translator` if installed)
