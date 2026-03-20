# Podcast Publishing Guide

## Prerequisites

- **ffmpeg** — required by pydub for M4A (AAC) encoding. Install via `brew install ffmpeg`.
- **Show artwork** — JPEG or PNG, 1400-3000px square. Required by Apple Podcasts.
- **Linode server** — or any web server with HTTPS for hosting the RSS feed and audio files.
- **mutagen** — Python library for M4A metadata tagging. Installed as a project dependency.

## Config Reference

Each podcast has a `publish:` section in `config.yml`:

```yaml
podcasts:
  crosswire:
    title: "Crosswire"
    host_name: "Angela"
    # ... other podcast config ...
    publish:
      base_url: "https://podcasts.getpagespeed.com/crosswire"
      publish_dir: "./publish/crosswire"
      m4a_bitrate: "128k"
      show_artwork: "./podcasts/assets/crosswire_artwork.jpg"
      show_description: "Daily geopolitics, tech, and science."
      show_category: "News"
      show_subcategory: "Daily News"
      explicit: false
      sync_command: "rsync -avz ./publish/crosswire/ podcasts@web.getpagespeed.com:/srv/www/podcasts.getpagespeed.com/httpdocs/crosswire/"
```

| Key                | Required | Default     | Description                                            |
|--------------------|----------|-------------|--------------------------------------------------------|
| `base_url`         | Yes      | —           | Public URL root for feed and episodes                  |
| `publish_dir`      | No       | `./publish/{name}` | Local directory for generated files              |
| `m4a_bitrate`      | No       | `128k`      | AAC encoding bitrate                                   |
| `show_artwork`     | No       | —           | Path to show artwork (JPEG/PNG, 1400-3000px square)    |
| `show_description` | No       | `""`        | Show description (up to 4000 chars for Apple)          |
| `show_category`    | No       | `News`      | iTunes top-level category                              |
| `show_subcategory` | No       | —           | iTunes subcategory                                     |
| `explicit`         | No       | `false`     | Whether the show contains explicit content             |
| `sync_command`     | No       | —           | Shell command to deploy files (e.g., rsync)            |

## First Episode Setup

1. **Create artwork** — 3000x3000px JPEG/PNG recommended. Place it in `podcasts/assets/`.

2. **Add publish config** to your podcast in `config.yml` (see above).

3. **Run the digest pipeline**:
   ```bash
   telegram-translator digest run --podcast crosswire
   ```

4. **Publish the episode**:
   ```bash
   telegram-translator digest publish --podcast crosswire
   ```
   This encodes the WAV to M4A, builds the RSS feed, copies artwork, and runs the sync command.

5. **Verify the feed** at your `base_url/feed.xml`. Validate with:
   - https://castfeedvalidator.com
   - Apple's Podcasts Connect validator

6. **Submit to Apple Podcasts**:
   - Go to https://podcasters.apple.com
   - Sign in with your Apple ID
   - Submit your RSS feed URL
   - Apple validates artwork, feed format, and at least 1 episode
   - Show typically appears within 24-48 hours

## Daily Workflow

```bash
# Collect + summarize + generate audio
telegram-translator digest run --podcast crosswire

# Review the output, then publish
telegram-translator digest publish --podcast crosswire
```

Or step by step:
```bash
telegram-translator digest collect
telegram-translator digest summarize --podcast crosswire
telegram-translator digest podcast --podcast crosswire
telegram-translator digest publish --podcast crosswire
```

## Feed-Only Rebuild

To fix metadata without re-encoding audio:
```bash
telegram-translator digest feed --podcast crosswire
```

Then re-sync manually or run the sync command.

## Publish Directory Structure

```
publish/
  crosswire/
    feed.xml
    crosswire_artwork.jpg
    episodes/
      crosswire_2026-03-19.m4a
      crosswire_2026-03-20.m4a
```

## RSS Feed Format

The generated feed follows RSS 2.0 with iTunes namespace extensions:

- `<itunes:type>episodic</itunes:type>` — episodes are standalone, not serial
- `<itunes:image>` — show artwork URL
- `<itunes:category>` — with optional subcategory
- `<itunes:duration>` — formatted as HH:MM:SS
- `<enclosure>` — M4A file URL with `type="audio/x-m4a"`
- `<guid>` — `{podcast_name}-{date}`, `isPermaLink=false`
- Episode description uses the executive summary (truncated to 4000 chars)

## Linode Server Setup

The nginx vhost and server configuration is managed via the Ansible playbook in `~/Projects/ansible`. Refer to that repository for the actual server setup.

The sync command in the podcast config uses rsync to deploy files to the server.

## Troubleshooting

### Feed validation errors

- **Missing artwork**: Ensure `show_artwork` path is correct and the image is 1400-3000px square JPEG/PNG.
- **Missing enclosure**: The episode M4A must exist at the expected path. Check `digest status`.
- **Invalid duration**: Duration is computed from the WAV during encoding. If it shows 00:00:00, re-encode.

### Apple Podcasts rejection

Common reasons:
- Artwork too small (must be at least 1400x1400px)
- Missing required iTunes tags (author, category, explicit)
- Feed not accessible over HTTPS
- No episodes in the feed

### Encoding issues

- Ensure ffmpeg is installed: `brew install ffmpeg`
- Check that the WAV file is valid: `ffprobe path/to/file.wav`
- M4A output should be ~1.5-2MB for a 10-minute episode at 128kbps
