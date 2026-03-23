"""Podcast publishing pipeline: encode, build RSS feed, deploy."""

import html
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from telegram_translator.audio_encoder import encode_m4a
from telegram_translator.content_store import ContentStore
from telegram_translator.feed_generator import PodcastFeed, _markdown_to_html

logger = logging.getLogger(__name__)


class PodcastPublisher:
    """Encode M4A, rebuild RSS feed, and deploy podcast episodes."""

    def __init__(self, config: dict, store: ContentStore):
        """Initialize the publisher.

        Args:
            config: Resolved podcast config dict (includes publish section).
            store: ContentStore instance for digest records.
        """
        self.config = config
        self.store = store

    async def publish(
        self,
        podcast_name: str,
        date: str | None = None,
    ) -> str:
        """Encode M4A, rebuild RSS feed, and deploy.

        Args:
            podcast_name: Podcast identifier.
            date: Target date (YYYY-MM-DD). Defaults to today.

        Returns:
            Path to the M4A file.

        Raises:
            RuntimeError: If no audio exists or publish config is missing.
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        publish_cfg = self.config.get("publish", {})

        if not publish_cfg:
            raise RuntimeError(
                f"No publish config for podcast '{podcast_name}'. "
                "Add a 'publish:' section to the podcast config."
            )

        digest = self.store.get_digest(date, podcast_name)
        if not digest or not digest.audio_path:
            raise RuntimeError(
                f"No audio found for {podcast_name}/{date}. "
                "Run 'digest podcast' first."
            )

        wav_path = Path(digest.audio_path)
        if not wav_path.exists():
            raise RuntimeError(f"Audio file not found: {wav_path}")

        publish_dir = Path(
            publish_cfg.get(
                "publish_dir", f"./publish/{podcast_name}"
            )
        )
        episodes_dir = publish_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)

        # Encode WAV -> M4A
        m4a_filename = f"{podcast_name}_{date}.m4a"
        m4a_path = episodes_dir / m4a_filename
        bitrate = publish_cfg.get("m4a_bitrate", "128k")

        title = self.config.get("title", podcast_name)
        formatted_date = datetime.strptime(
            date, "%Y-%m-%d"
        ).strftime("%B %d, %Y")
        metadata = {
            "title": f"{title} \u2014 {formatted_date}",
            "artist": self.config.get("host_name", ""),
            "album": title,
            "date": date,
        }

        m4a_path, duration = encode_m4a(
            wav_path, m4a_path, bitrate, metadata
        )

        # Copy show artwork + generate thumbnail for HTML page
        artwork_src = publish_cfg.get("show_artwork")
        if artwork_src:
            artwork_src = Path(artwork_src)
            if artwork_src.exists():
                artwork_dst = publish_dir / artwork_src.name
                shutil.copy2(artwork_src, artwork_dst)
                self._generate_thumbnail(artwork_src, publish_dir)

        # Update digest record
        self.store.update_digest(
            date,
            podcast_name,
            m4a_path=str(m4a_path),
            duration_seconds=duration,
            published_at=datetime.now(tz=timezone.utc).isoformat(),
        )

        # Rebuild RSS feed
        self.rebuild_feed(podcast_name, publish_cfg)

        # Run sync command
        sync_command = publish_cfg.get("sync_command")
        if sync_command:
            logger.info("Running sync: %s", sync_command)
            result = subprocess.run(
                sync_command,
                shell=True,  # noqa: S602
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error("Sync failed: %s", result.stderr)
            else:
                logger.info("Sync complete")

        # Mark selected content items as used
        if digest.selected_item_ids:
            try:
                item_ids = json.loads(digest.selected_item_ids)
                self.store.mark_items_used(date, podcast_name, item_ids)
            except (json.JSONDecodeError, TypeError):
                logger.error(
                    "Failed to parse selected_item_ids for %s/%s",
                    podcast_name, date, exc_info=True,
                )

        logger.info(
            "Published %s/%s -> %s", podcast_name, date, m4a_path
        )
        return str(m4a_path)

    @staticmethod
    def _generate_thumbnail(
        artwork_src: Path,
        publish_dir: Path,
        size: int = 400,
    ) -> Path | None:
        """Generate a small JPEG thumbnail of the show artwork.

        Args:
            artwork_src: Path to the full-resolution artwork.
            publish_dir: Output directory.
            size: Thumbnail width/height in pixels.

        Returns:
            Path to the generated thumbnail, or None on failure.
        """
        thumb_path = publish_dir / "artwork_thumb.jpg"
        try:
            from PIL import Image

            with Image.open(artwork_src) as img:
                img = img.convert("RGB")
                img.thumbnail((size, size), Image.LANCZOS)
                img.save(thumb_path, "JPEG", quality=80, optimize=True)
            logger.info(
                "Generated artwork thumbnail: %s (%d bytes)",
                thumb_path,
                thumb_path.stat().st_size,
            )
            return thumb_path
        except Exception:
            logger.error(
                "Failed to generate artwork thumbnail", exc_info=True,
            )
            return None

    @staticmethod
    def _artwork_url(base_url: str, publish_cfg: dict) -> str:
        """Derive the full-res artwork URL for the RSS feed."""
        artwork_src = publish_cfg.get("show_artwork", "")
        if artwork_src and base_url:
            return f"{base_url}/{Path(artwork_src).name}"
        return f"{base_url}/artwork.jpg" if base_url else ""

    @staticmethod
    def _thumbnail_url(base_url: str) -> str:
        """Derive the thumbnail artwork URL for the HTML page."""
        return f"{base_url}/artwork_thumb.jpg" if base_url else ""

    def rebuild_feed(
        self,
        podcast_name: str,
        publish_cfg: dict | None = None,
    ) -> Path:
        """Rebuild the RSS feed from all published episodes.

        Args:
            podcast_name: Podcast identifier.
            publish_cfg: Publish config dict. Uses self.config if None.

        Returns:
            Path to the generated feed.xml.
        """
        publish_cfg = publish_cfg or self.config.get("publish", {})
        base_url = publish_cfg.get("base_url", "")
        publish_dir = Path(
            publish_cfg.get(
                "publish_dir", f"./publish/{podcast_name}"
            )
        )

        title = self.config.get("title", podcast_name)

        feed = PodcastFeed(
            title=title,
            base_url=base_url,
            description=publish_cfg.get("show_description", ""),
            author=self.config.get("host_name", ""),
            language=self.config.get("language", "en"),
            category=publish_cfg.get("show_category", "News"),
            subcategory=publish_cfg.get("show_subcategory", ""),
            artwork_url=self._artwork_url(base_url, publish_cfg),
            explicit=publish_cfg.get("explicit", False),
            feed_url=f"{base_url}/feed.xml",
            copyright_text=publish_cfg.get("copyright", ""),
        )

        # Query all published episodes
        digests = self.store.list_digests(
            limit=1000, podcast_name=podcast_name
        )
        episodes = []
        for d in digests:
            if not d.m4a_path or not d.published_at:
                continue

            m4a_file = Path(d.m4a_path)
            formatted_date = datetime.strptime(
                d.date, "%Y-%m-%d"
            ).strftime("%B %d, %Y")
            file_size = (
                m4a_file.stat().st_size if m4a_file.exists() else 0
            )

            episodes.append({
                "title": f"{title} \u2014 {formatted_date}",
                "description": (
                    d.executive_summary[:4000]
                    if d.executive_summary
                    else ""
                ),
                "executive_summary": d.executive_summary or "",
                "filename": m4a_file.name,
                "duration_seconds": d.duration_seconds,
                "pub_date": d.date,
                "guid": f"{podcast_name}-{d.date}",
                "file_size": file_size,
            })

        feed_path = publish_dir / "feed.xml"
        feed.generate(episodes, feed_path)

        # Build HTML index page
        self._build_index_html(
            podcast_name, episodes, publish_dir, publish_cfg,
        )

        return feed_path

    def _build_index_html(
        self,
        podcast_name: str,
        episodes: list[dict],
        publish_dir: Path,
        publish_cfg: dict,
    ):
        """Generate a static HTML index page listing all episodes.

        Args:
            podcast_name: Podcast identifier.
            episodes: Episode dicts (same as passed to feed generator).
            publish_dir: Output directory.
            publish_cfg: Publish config dict.
        """
        title = self.config.get("title", podcast_name)
        description = publish_cfg.get("show_description", "")
        base_url = publish_cfg.get("base_url", "")
        artwork_url = self._thumbnail_url(base_url)
        author = self.config.get("host_name", "")
        copyright_text = publish_cfg.get("copyright", "")

        sorted_eps = sorted(
            episodes,
            key=lambda e: e.get("pub_date", ""),
            reverse=True,
        )

        episode_cards = []
        for ep in sorted_eps:
            dur = ep.get("duration_seconds", 0)
            if dur:
                mins, secs = divmod(int(dur), 60)
                dur_str = f"{mins}:{secs:02d}"
            else:
                dur_str = ""

            filename = ep.get("filename", "")
            audio_url = f"{base_url}/episodes/{filename}" if filename else ""
            ep_title = html.escape(ep.get("title", "Untitled"))

            summary_html = ""
            summary = ep.get("executive_summary", "")
            if summary:
                summary_html = _markdown_to_html(summary)

            episode_cards.append(
                f'<article class="episode">\n'
                f'  <h2>{ep_title}</h2>\n'
                f'  <div class="meta">\n'
                f'    <time>{html.escape(ep.get("pub_date", ""))}</time>\n'
                + (f'    <span class="duration">{dur_str}</span>\n' if dur_str else "")
                + f'  </div>\n'
                + (
                    f'  <audio controls preload="none">\n'
                    f'    <source src="{html.escape(audio_url)}" type="audio/x-m4a">\n'
                    f'  </audio>\n'
                    if audio_url else ""
                )
                + (
                    f'  <details>\n'
                    f'    <summary>Show notes</summary>\n'
                    f'    <div class="show-notes">{summary_html}</div>\n'
                    f'  </details>\n'
                    if summary_html else ""
                )
                + (
                    f'  <a class="download" href="{html.escape(audio_url)}" '
                    f'download>Download</a>\n'
                    if audio_url else ""
                )
                + f'</article>'
            )

        episodes_html = "\n".join(episode_cards)

        page = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{
  margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  "Helvetica Neue",Arial,sans-serif;background:#0a0a0a;color:#e0e0e0;
  line-height:1.6;
}}
a{{color:#6cb4ee;text-decoration:none}}
a:hover{{text-decoration:underline}}
.container{{max-width:720px;margin:0 auto;padding:2rem 1.5rem}}
header{{text-align:center;margin-bottom:3rem}}
header img{{
  width:200px;height:200px;border-radius:16px;
  box-shadow:0 4px 24px rgba(0,0,0,.5);margin-bottom:1.5rem;
}}
header h1{{margin:0 0 .5rem;font-size:1.8rem;color:#fff}}
header p{{margin:0 0 1rem;color:#999;font-size:1rem}}
.subscribe{{
  display:inline-block;padding:.5rem 1.2rem;border:1px solid #333;
  border-radius:8px;font-size:.85rem;color:#aaa;transition:border-color .2s;
}}
.subscribe:hover{{border-color:#6cb4ee;color:#6cb4ee;text-decoration:none}}
.episode{{
  background:#151515;border:1px solid #222;border-radius:12px;
  padding:1.5rem;margin-bottom:1.5rem;
}}
.episode h2{{margin:0 0 .5rem;font-size:1.15rem;color:#fff}}
.meta{{display:flex;gap:1rem;font-size:.85rem;color:#777;margin-bottom:1rem}}
audio{{width:100%;margin-bottom:.75rem;border-radius:8px}}
details{{margin-bottom:.75rem}}
summary{{
  cursor:pointer;font-size:.85rem;color:#888;padding:.25rem 0;
}}
summary:hover{{color:#aaa}}
.show-notes{{
  margin-top:.75rem;font-size:.9rem;color:#bbb;line-height:1.7;
}}
.show-notes h3{{font-size:1rem;color:#ddd;margin:1.25rem 0 .5rem}}
.show-notes p{{margin:0 0 .75rem}}
.show-notes ul{{margin:0 0 .75rem;padding-left:1.5rem}}
.show-notes li{{margin-bottom:.25rem}}
.show-notes strong{{color:#ddd}}
.download{{
  display:inline-block;font-size:.8rem;color:#555;
  border:1px solid #333;border-radius:6px;padding:.3rem .8rem;
}}
.download:hover{{color:#6cb4ee;border-color:#6cb4ee;text-decoration:none}}
footer{{text-align:center;padding:2rem 0;color:#444;font-size:.8rem}}
</style>
</head>
<body>
<div class="container">
  <header>
    <img src="{html.escape(artwork_url)}" alt="{html.escape(title)} artwork">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(description)}</p>
    <p>Hosted by {html.escape(author)}</p>
    <a id="subscribe-btn" class="subscribe" href="{html.escape(base_url)}/feed.xml">Subscribe via RSS</a>
    <script>
    (function(){{
      if (/iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent)) {{
        var a = document.getElementById('subscribe-btn');
        a.href = a.href.replace(/^https?:\\/\\//, 'podcast://');
        a.textContent = 'Subscribe';
      }}
    }})();
    </script>
  </header>
  <main>
{episodes_html}
  </main>
  <footer>{html.escape(copyright_text)}</footer>
</div>
</body>
</html>"""

        index_path = publish_dir / "index.html"
        index_path.write_text(page, encoding="utf-8")
        logger.info("Index page written: %s", index_path)
