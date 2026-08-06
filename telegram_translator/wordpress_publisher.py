"""Idempotent WordPress publisher for Seriously Simple Podcasting."""

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from telegram_translator.audio_encoder import encode_m4a


logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    """Return a conservative ASCII WordPress slug."""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "episode"


def _duration_text(seconds: float) -> str:
    """Format seconds as an Apple-compatible HH:MM:SS duration."""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _filesize_text(size: int) -> str:
    """Format a byte count like the SSP admin UI."""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 2
            return f"{value:.{precision}f}{unit}"
        value /= 1024
    return f"{size}B"


class WordPressPodcastPublisher:
    """Publish generated audio through WordPress REST without duplicates."""

    def __init__(self, config: dict, store):
        """Initialize a publisher from a resolved podcast config."""
        self.config = config
        self.store = store
        self.publish_cfg = config.get("publish", {})

    async def publish(
        self,
        podcast_name: str,
        date: str,
        digest,
        wav_path: Path,
    ) -> str:
        """Encode, upload, and publish one WordPress podcast episode.

        The WordPress post is created as a draft before media upload. Retries
        reuse both that deterministic draft and any audio already attached to
        it, so a partial failure cannot produce duplicate public episodes.
        """
        article = self._selected_article(digest)
        title = article.title.strip()
        source_url = article.url.strip()
        source_slug = Path(urlparse(source_url).path.rstrip("/")).name
        episode_slug = f"{_slugify(source_slug or title)}-audio"

        publish_dir = Path(
            self.publish_cfg.get("publish_dir", f"./publish/{podcast_name}")
        )
        episodes_dir = publish_dir / podcast_name / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        m4a_path = episodes_dir / f"{episode_slug}.m4a"

        metadata = {
            "title": title,
            "artist": self.config.get("host_name", "GetPageSpeed"),
            "album": self.config.get("title", podcast_name),
            "date": date,
        }
        bitrate = self.publish_cfg.get("m4a_bitrate", "96k")
        loudness_target = self.publish_cfg.get("loudness_target_lufs")
        m4a_path, duration = encode_m4a(
            wav_path,
            m4a_path,
            bitrate,
            metadata,
            (
                float(loudness_target)
                if loudness_target is not None
                else None
            ),
        )

        username, application_password = self._credentials()
        base_url = self.publish_cfg["base_url"].rstrip("/")
        post_type = self.publish_cfg.get("post_type", "podcast")
        api_root = f"{base_url}/wp-json/wp/v2"
        auth = httpx.BasicAuth(username, application_password)

        show_notes = self._show_notes(digest, title, source_url)
        excerpt = self._excerpt(digest.executive_summary or title)

        async with httpx.AsyncClient(
            auth=auth,
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "GetPageSpeed-Podcast/1.0"},
        ) as client:
            episode = await self._find_episode(
                client, api_root, post_type, episode_slug
            )
            if episode is None:
                episode = await self._create_draft(
                    client,
                    api_root,
                    post_type,
                    episode_slug,
                    title,
                    show_notes,
                    excerpt,
                )

            episode_id = int(episode["id"])
            media_url = self._episode_audio_url(episode)
            if not media_url:
                media_url = await self._find_attached_audio(
                    client, api_root, episode_id, m4a_path.stem
                )
            if not media_url:
                media_url = await self._upload_audio(
                    client, api_root, episode_id, m4a_path
                )

            status = str(self.publish_cfg.get("post_status", "publish"))
            updated = await self._finalize_episode(
                client=client,
                api_root=api_root,
                post_type=post_type,
                episode_id=episode_id,
                title=title,
                show_notes=show_notes,
                excerpt=excerpt,
                media_url=media_url,
                m4a_path=m4a_path,
                duration=duration,
                date=date,
                status=status,
            )
            self._verify_episode(updated, media_url, status)

        self.store.update_digest(
            date,
            podcast_name,
            m4a_path=str(m4a_path),
            duration_seconds=duration,
            published_at=datetime.now(tz=timezone.utc).isoformat(),
            status="published" if status == "publish" else status,
        )
        logger.info(
            "Published WordPress episode %s for article %s",
            episode_slug,
            source_url,
        )
        return str(m4a_path)

    def _selected_article(self, digest):
        """Resolve the single article selected during summarization."""
        try:
            item_ids = [
                int(value) for value in json.loads(digest.selected_item_ids or "[]")
            ]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Digest has invalid selected_item_ids") from exc

        items = self.store.get_content_items_by_ids(item_ids)
        articles = [item for item in items if item.title and item.url]
        if len(articles) != 1:
            raise RuntimeError(
                "WordPress podcast publishing requires exactly one selected "
                f"article; found {len(articles)}"
            )
        return articles[0]

    def _credentials(self) -> tuple[str, str]:
        """Load WordPress application-password credentials from env vars."""
        username_env = self.publish_cfg.get("username_env", "")
        password_env = self.publish_cfg.get("application_password_env", "")
        username = os.environ.get(username_env, "") if username_env else ""
        password = os.environ.get(password_env, "") if password_env else ""
        if not username or not password:
            raise RuntimeError(
                "WordPress credentials are unavailable. Set the env vars "
                f"named by username_env/application_password_env "
                f"({username_env!r}, {password_env!r})."
            )
        return username, password

    @staticmethod
    def _show_notes(digest, title: str, source_url: str) -> str:
        """Build concise Markdown show notes with an article conversion link."""
        summary = (digest.executive_summary or "").strip()
        parts = [summary] if summary else []
        parts.append(f"[Read the full article: {title}]({source_url})")
        return "\n\n".join(parts)

    @staticmethod
    def _excerpt(value: str, limit: int = 500) -> str:
        """Build a compact plain-text episode excerpt."""
        plain = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
        plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
        plain = re.sub(
            r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-+*]\s+|\d+[.)]\s+)",
            "",
            plain,
        )
        plain = re.sub(r"[*_~`]", "", plain)
        plain = re.sub(r"<[^>]+>", " ", plain)
        plain = re.sub(r"\s+", " ", html.unescape(plain)).strip()
        if len(plain) <= limit:
            return plain
        return plain[: limit - 1].rstrip() + "…"

    async def _find_episode(
        self,
        client: httpx.AsyncClient,
        api_root: str,
        post_type: str,
        slug: str,
    ) -> dict | None:
        response = await client.get(
            f"{api_root}/{post_type}",
            params={"slug": slug, "context": "edit", "status": "any"},
        )
        self._raise_for_status(response, "find episode")
        rows = response.json()
        return rows[0] if rows else None

    async def _create_draft(
        self,
        client: httpx.AsyncClient,
        api_root: str,
        post_type: str,
        slug: str,
        title: str,
        content: str,
        excerpt: str,
    ) -> dict:
        payload = {
            "slug": slug,
            "title": title,
            "content": content,
            "excerpt": excerpt,
            "status": "draft",
            "series": [int(self.publish_cfg["series_id"])],
        }
        response = await client.post(f"{api_root}/{post_type}", json=payload)
        self._raise_for_status(response, "create episode draft")
        return response.json()

    @staticmethod
    def _episode_audio_url(episode: dict) -> str:
        meta = episode.get("meta") or {}
        return str(meta.get("audio_file", "")).strip()

    async def _find_attached_audio(
        self,
        client: httpx.AsyncClient,
        api_root: str,
        episode_id: int,
        media_slug: str,
    ) -> str:
        response = await client.get(
            f"{api_root}/media",
            params={
                "parent": episode_id,
                "per_page": 100,
                "media_type": "audio",
                "context": "edit",
            },
        )
        self._raise_for_status(response, "find attached audio")
        for media in response.json():
            if media.get("slug") == media_slug or media.get("source_url", "").endswith(
                f"/{media_slug}.m4a"
            ):
                return str(media.get("source_url", ""))
        return ""

    async def _upload_audio(
        self,
        client: httpx.AsyncClient,
        api_root: str,
        episode_id: int,
        m4a_path: Path,
    ) -> str:
        headers = {
            "Content-Disposition": (f'attachment; filename="{m4a_path.name}"'),
            "Content-Type": "audio/mp4",
        }
        response = await client.post(
            f"{api_root}/media",
            params={"post": episode_id},
            headers=headers,
            content=m4a_path.read_bytes(),
        )
        self._raise_for_status(response, "upload episode audio")
        media_url = str(response.json().get("source_url", ""))
        if not media_url:
            raise RuntimeError("WordPress media upload returned no source_url")
        return media_url

    async def _finalize_episode(
        self,
        *,
        client: httpx.AsyncClient,
        api_root: str,
        post_type: str,
        episode_id: int,
        title: str,
        show_notes: str,
        excerpt: str,
        media_url: str,
        m4a_path: Path,
        duration: float,
        date: str,
        status: str,
    ) -> dict:
        size = m4a_path.stat().st_size
        payload = {
            "title": title,
            "content": show_notes,
            "excerpt": excerpt,
            "status": status,
            "series": [int(self.publish_cfg["series_id"])],
            "meta": {
                "episode_type": "audio",
                "audio_file": media_url,
                "duration": _duration_text(duration),
                "filesize": _filesize_text(size),
                "filesize_raw": str(size),
                "date_recorded": date,
                "explicit": "on" if self.publish_cfg.get("explicit") else "",
                "block": "",
                "itunes_title": title,
                "itunes_episode_type": "full",
            },
        }
        response = await client.post(
            f"{api_root}/{post_type}/{episode_id}", json=payload
        )
        self._raise_for_status(response, "finalize episode")
        return response.json()

    @staticmethod
    def _verify_episode(episode: dict, media_url: str, status: str) -> None:
        """Verify the authoritative REST response after finalization."""
        actual_status = episode.get("status")
        actual_audio = str((episode.get("meta") or {}).get("audio_file", ""))
        if actual_status != status or actual_audio != media_url:
            raise RuntimeError(
                "WordPress episode verification failed: "
                f"status={actual_status!r}, audio_file={actual_audio!r}"
            )

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        """Raise a concise error that does not expose credentials."""
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text[:500].replace("\n", " ")
            raise RuntimeError(
                f"WordPress failed to {action}: HTTP {response.status_code}: {body}"
            ) from exc
