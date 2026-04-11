"""Podcast publishing pipeline: encode, build RSS feed, deploy."""

import html
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from telegram_translator.audio_encoder import encode_m4a
from telegram_translator.content_store import ContentStore
from telegram_translator.feed_generator import PodcastFeed, _markdown_to_html

logger = logging.getLogger(__name__)


_SITE_CSS = """\
*,*::before,*::after{box-sizing:border-box}
body{
  margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  "Helvetica Neue",Arial,sans-serif;background:#0a0a0a;color:#e0e0e0;
  line-height:1.6;
}
a{color:#6cb4ee;text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:720px;margin:0 auto;padding:2rem 1.5rem}
header{text-align:center;margin-bottom:3rem}
header img{
  width:200px;height:200px;border-radius:16px;
  box-shadow:0 4px 24px rgba(0,0,0,.5);margin-bottom:1.5rem;
}
header h1{margin:0 0 .5rem;font-size:1.8rem;color:#fff}
header p{margin:0 0 1rem;color:#999;font-size:1rem}
.subscribe{
  display:inline-block;padding:.5rem 1.2rem;border:1px solid #333;
  border-radius:8px;font-size:.85rem;color:#aaa;transition:border-color .2s;
}
.subscribe:hover{border-color:#6cb4ee;color:#6cb4ee;text-decoration:none}
.episode{
  background:#151515;border:1px solid #222;border-radius:12px;
  padding:1.5rem;margin-bottom:1.5rem;
}
.episode h2{margin:0 0 .5rem;font-size:1.15rem;color:#fff}
.meta{display:flex;gap:1rem;font-size:.85rem;color:#777;margin-bottom:1rem}
audio{width:100%;margin-bottom:.75rem;border-radius:8px}
details{margin-bottom:.75rem}
summary{
  cursor:pointer;font-size:.85rem;color:#888;padding:.25rem 0;
}
summary:hover{color:#aaa}
.show-notes{
  margin-top:.75rem;font-size:.9rem;color:#bbb;line-height:1.7;
}
.show-notes h3{font-size:1rem;color:#ddd;margin:1.25rem 0 .5rem}
.show-notes p{margin:0 0 .75rem}
.show-notes ul{margin:0 0 .75rem;padding-left:1.5rem}
.show-notes li{margin-bottom:.25rem}
.show-notes strong{color:#ddd}
.download{
  display:inline-block;font-size:.8rem;color:#555;
  border:1px solid #333;border-radius:6px;padding:.3rem .8rem;
}
.download:hover{color:#6cb4ee;border-color:#6cb4ee;text-decoration:none}
.podcast-grid{
  display:grid;grid-template-columns:1fr;gap:1.5rem;
}
.podcast-card{
  background:#151515;border:1px solid #222;border-radius:12px;
  padding:1.5rem;display:flex;gap:1.25rem;align-items:flex-start;
}
.podcast-card img{
  width:96px;height:96px;border-radius:8px;flex-shrink:0;
}
.podcast-card .info{flex:1;min-width:0}
.podcast-card h2{margin:0 0 .4rem;font-size:1.1rem;color:#fff}
.podcast-card h2 a{color:inherit}
.podcast-card h2 a:hover{color:#6cb4ee;text-decoration:none}
.podcast-card .desc{
  margin:0 0 .5rem;font-size:.9rem;color:#aaa;
}
.podcast-card .latest{
  font-size:.75rem;color:#666;margin-bottom:.6rem;
}
.podcast-card .actions{display:flex;gap:.5rem;flex-wrap:wrap}
.podcast-card .actions a{
  display:inline-block;font-size:.75rem;color:#777;
  border:1px solid #333;border-radius:6px;padding:.25rem .65rem;
}
.podcast-card .actions a:hover{color:#6cb4ee;border-color:#6cb4ee;text-decoration:none}
footer{text-align:center;padding:2rem 0;color:#444;font-size:.8rem}
"""


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

        Branches on the resolved destination type:
        - ``static`` or legacy (no destination): encode + rebuild feed +
          per-podcast HTML index + (legacy only) per-podcast sync command.
        - ``astro_collection``: encode m4a + copy artwork + write episode
          Markdown into the Astro content collection. No feed, no HTML.

        Destination-scoped podcasts (both types) skip running the
        destination sync command here — the CLI runs it once after all
        podcasts in a destination have been rebuilt.

        Args:
            podcast_name: Podcast identifier.
            date: Target date (YYYY-MM-DD). Defaults to today.

        Returns:
            Path to the generated M4A file.

        Raises:
            RuntimeError: If no audio exists or publish config is missing.
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        publish_cfg = self.config.get("publish", {})
        destination_type = self.config.get("destination_type")

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

        if destination_type == "astro_collection":
            m4a_path = await self._publish_astro(
                podcast_name, date, digest, wav_path, publish_cfg
            )
        else:
            m4a_path = await self._publish_static(
                podcast_name, date, digest, wav_path, publish_cfg
            )

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

    async def _publish_static(
        self,
        podcast_name: str,
        date: str,
        digest,
        wav_path: Path,
        publish_cfg: dict,
    ) -> str:
        """Publish to a static destination (or legacy per-podcast config).

        Args:
            podcast_name: Podcast identifier.
            date: Target date (YYYY-MM-DD).
            digest: Current digest record.
            wav_path: Source WAV file.
            publish_cfg: Resolved publish config dict.

        Returns:
            Path to the generated M4A file.
        """
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

        # Rebuild RSS feed + per-podcast HTML index
        self.rebuild_feed(podcast_name, publish_cfg)

        # Run per-podcast sync command only for LEGACY podcasts (no
        # destination). Destination-scoped podcasts defer the sync to the
        # CLI, which runs the destination sync exactly once per destination.
        if not self.config.get("destination_name"):
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

        return str(m4a_path)

    async def _publish_astro(
        self,
        podcast_name: str,
        date: str,
        digest,
        wav_path: Path,
        publish_cfg: dict,
    ) -> str:
        """Publish to an astro_collection destination (Vaske-style).

        Writes episode m4a + artwork to ``public_dir`` and an episode
        Markdown file into ``content_dir``. Does not generate any HTML or
        RSS feed — those are rendered by the downstream Astro build.

        Args:
            podcast_name: Podcast identifier.
            date: Target date (YYYY-MM-DD).
            digest: Current digest record.
            wav_path: Source WAV file.
            publish_cfg: Resolved publish config dict (includes
                ``content_dir`` and ``public_dir``).

        Returns:
            Path to the generated M4A file.

        Raises:
            RuntimeError: If required destination keys are missing.
        """
        content_dir = publish_cfg.get("content_dir")
        public_dir = publish_cfg.get("public_dir")
        if not content_dir or not public_dir:
            raise RuntimeError(
                f"astro_collection publish config for "
                f"'{podcast_name}' missing content_dir/public_dir"
            )

        content_dir_path = Path(content_dir)
        public_dir_path = Path(public_dir)
        episodes_dir = public_dir_path / "episodes"
        content_dir_path.mkdir(parents=True, exist_ok=True)
        episodes_dir.mkdir(parents=True, exist_ok=True)

        # Encode WAV -> M4A into Astro's public tree
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

        # Copy show artwork into public tree
        artwork_src = publish_cfg.get("show_artwork")
        if artwork_src:
            artwork_src_path = Path(artwork_src)
            if artwork_src_path.exists():
                shutil.copy2(
                    artwork_src_path,
                    public_dir_path / artwork_src_path.name,
                )
                self._generate_thumbnail(
                    artwork_src_path, public_dir_path
                )

        # Update digest record so subsequent publishes/feeds see it
        self.store.update_digest(
            date,
            podcast_name,
            m4a_path=str(m4a_path),
            duration_seconds=duration,
            published_at=datetime.now(tz=timezone.utc).isoformat(),
        )

        # Derive the audioUrl relative to the site root. Strip a leading
        # '<site_host>' from the base_url and append /episodes/<filename>
        # under that path so it resolves in Astro dev and prod.
        audio_url = self._astro_audio_url(
            publish_cfg.get("base_url", ""), m4a_filename
        )

        # Write the episode Markdown into the Astro content collection
        self._write_astro_episode(
            podcast_name=podcast_name,
            date=date,
            title=title,
            formatted_date=formatted_date,
            duration=duration,
            audio_url=audio_url,
            executive_summary=digest.executive_summary or "",
            content_dir=content_dir_path,
        )

        return str(m4a_path)

    @staticmethod
    def _astro_audio_url(base_url: str, filename: str) -> str:
        """Derive a site-root-relative audio URL for Astro.

        Given a destination ``base_url`` like
        ``https://www.vaske.ru/podcast``, returns ``/podcast/episodes/<filename>``
        so the same path works in Astro dev and prod.

        Args:
            base_url: Destination base URL (normalized, no trailing slash).
            filename: M4A filename (no path components).

        Returns:
            Site-root-relative audio URL string.
        """
        if not base_url:
            return f"/episodes/{filename}"
        # Strip scheme + host, keep path only
        stripped = re.sub(r"^https?://[^/]+", "", base_url)
        if not stripped:
            return f"/episodes/{filename}"
        return f"{stripped.rstrip('/')}/episodes/{filename}"

    @staticmethod
    def _write_astro_episode(
        podcast_name: str,
        date: str,
        title: str,
        formatted_date: str,
        duration: float,
        audio_url: str,
        executive_summary: str,
        content_dir: Path,
    ) -> Path:
        """Write an episode Markdown file for an Astro content collection.

        Args:
            podcast_name: Podcast identifier (used in the filename).
            date: Target date (YYYY-MM-DD).
            title: Podcast show title.
            formatted_date: Human-readable date (e.g., "April 11, 2026").
            duration: Episode duration in seconds.
            audio_url: Site-root-relative audio URL.
            executive_summary: Full episode show notes as Markdown.
            content_dir: Directory where the episode file will be written.

        Returns:
            Path to the written Markdown file.
        """
        filename = f"{podcast_name}-{date}.md"
        ep_path = content_dir / filename

        # Derive a short plain-text description (first ~200 chars, no md)
        flat = re.sub(r"[#*_`>\-]+", "", executive_summary or "")
        flat = re.sub(r"\s+", " ", flat).strip()
        description = flat[:200].rstrip()
        if len(flat) > 200:
            description += "\u2026"

        # Escape YAML double quotes in string values
        def _q(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        frontmatter = (
            "---\n"
            f'title: "{_q(title)} \u2014 {_q(formatted_date)}"\n'
            f'description: "{_q(description)}"\n'
            f"date: {date}\n"
            f'audioUrl: "{_q(audio_url)}"\n'
            f"duration: {int(round(duration))}\n"
            "---\n\n"
        )

        ep_path.write_text(
            frontmatter + (executive_summary or ""), encoding="utf-8"
        )
        logger.info("Wrote Astro episode: %s", ep_path)
        return ep_path

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
            owner_name=publish_cfg.get("owner_name", ""),
            owner_email=publish_cfg.get("owner_email", ""),
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
        spotify_url = publish_cfg.get("spotify_url", "")

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

        spotify_link = (
            f'      <a class="subscribe" '
            f'href="{html.escape(spotify_url)}">Listen on Spotify</a>\n'
            if spotify_url
            else ""
        )
        page = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{_SITE_CSS}</style>
</head>
<body>
<div class="container">
  <header>
    <img src="{html.escape(artwork_url)}" alt="{html.escape(title)} artwork">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(description)}</p>
    <p>Hosted by {html.escape(author)}</p>
    <div style="display:flex;gap:.75rem;justify-content:center;flex-wrap:wrap;margin-top:.5rem">
      <a id="subscribe-btn" class="subscribe" href="{html.escape(base_url)}/feed.xml">Subscribe via RSS</a>
{spotify_link}    </div>
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

    @classmethod
    def build_static_site_index(
        cls,
        destination_name: str,
        destination_cfg: dict,
        podcast_configs: list[dict],
        store: ContentStore,
    ) -> Path | None:
        """Write the root landing page for a static destination.

        Generates ``{destination.publish_dir}/index.html`` listing every
        podcast on the destination. Each card shows artwork, title,
        description, latest episode date, and links to the per-podcast page
        and RSS feed.

        Skipped (returns None) when the destination only has a single
        root-mounted podcast (``slug == ""``) — the per-podcast page already
        lives at the destination root.

        Args:
            destination_name: Destination identifier (for logging).
            destination_cfg: Resolved destination config dict.
            podcast_configs: List of resolved podcast config dicts bound to
                this destination.
            store: ContentStore, used to look up latest-episode dates.

        Returns:
            Path to the generated index.html, or None if skipped.
        """
        if destination_cfg.get("type") != "static":
            return None

        publish_dir = Path(destination_cfg["publish_dir"])
        # Skip root index for a single root-mounted podcast; the per-podcast
        # page already sits at publish_dir/index.html.
        if len(podcast_configs) == 1 and podcast_configs[0].get("slug") == "":
            logger.info(
                "Skipping root site index for destination '%s' "
                "(single root-mounted podcast)",
                destination_name,
            )
            return None

        site_title = destination_cfg.get("site_title") or destination_name
        site_description = destination_cfg.get("site_description", "")
        copyright_text = destination_cfg.get("copyright", "")

        cards: list[str] = []
        for pcfg in sorted(
            podcast_configs, key=lambda p: p.get("title", p["name"]).lower()
        ):
            slug = pcfg.get("slug") or pcfg["name"]
            name = pcfg["name"]
            pc_publish = pcfg.get("publish", {})
            title = pcfg.get("title", name)
            show_description = pc_publish.get("show_description", "")

            latest = store.list_digests(limit=1, podcast_name=name)
            latest_date = ""
            if latest and latest[0].published_at:
                latest_date = latest[0].date

            # Prefer the per-podcast thumbnail generated by _generate_thumbnail
            thumb_rel = f"{slug}/artwork_thumb.jpg"
            thumb_exists = (publish_dir / thumb_rel).exists()
            thumb_tag = (
                f'    <img src="{html.escape(thumb_rel)}" '
                f'alt="{html.escape(title)} artwork">\n'
                if thumb_exists
                else ""
            )

            desc_html = (
                _markdown_to_html(show_description)
                if show_description
                else ""
            )

            cards.append(
                f'  <article class="podcast-card">\n'
                f'{thumb_tag}'
                f'    <div class="info">\n'
                f'      <h2><a href="{html.escape(slug)}/">'
                f'{html.escape(title)}</a></h2>\n'
                + (
                    f'      <div class="desc">{desc_html}</div>\n'
                    if desc_html else ""
                )
                + (
                    f'      <div class="latest">Latest episode: '
                    f'{html.escape(latest_date)}</div>\n'
                    if latest_date else ""
                )
                + f'      <div class="actions">\n'
                f'        <a href="{html.escape(slug)}/">Open &rarr;</a>\n'
                f'        <a href="{html.escape(slug)}/feed.xml">RSS</a>\n'
                f'      </div>\n'
                f'    </div>\n'
                f'  </article>'
            )

        cards_html = "\n".join(cards)

        page = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(site_title)}</title>
<style>
{_SITE_CSS}</style>
</head>
<body>
<div class="container">
  <header>
    <h1>{html.escape(site_title)}</h1>
    <p>{html.escape(site_description)}</p>
  </header>
  <main class="podcast-grid">
{cards_html}
  </main>
  <footer>{html.escape(copyright_text)}</footer>
</div>
</body>
</html>"""

        publish_dir.mkdir(parents=True, exist_ok=True)
        index_path = publish_dir / "index.html"
        index_path.write_text(page, encoding="utf-8")
        logger.info(
            "Site index written for '%s': %s",
            destination_name,
            index_path,
        )
        return index_path

    @staticmethod
    def run_destination_sync(
        destination_name: str, destination_cfg: dict
    ) -> bool:
        """Run a destination's sync_command exactly once.

        Args:
            destination_name: Destination identifier (for logging).
            destination_cfg: Resolved destination config dict.

        Returns:
            True if the sync succeeded or no sync command was configured;
            False if the sync command failed.
        """
        sync_command = destination_cfg.get("sync_command")
        if not sync_command:
            logger.info(
                "No sync_command for destination '%s' — skipping",
                destination_name,
            )
            return True

        logger.info(
            "Running sync for destination '%s': %s",
            destination_name,
            sync_command,
        )
        result = subprocess.run(
            sync_command,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "Sync failed for destination '%s': %s",
                destination_name,
                result.stderr,
            )
            return False
        logger.info("Sync complete for destination '%s'", destination_name)
        return True
