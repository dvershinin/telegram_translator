"""SQLite-based content store for digest pipeline."""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ContentItem:
    """A collected content item from any source."""

    source_name: str
    source_type: str
    content: str
    title: Optional[str] = None
    url: Optional[str] = None
    message_id: Optional[int] = None
    content_hash: str = ""
    published_at: Optional[datetime] = None
    collected_at: Optional[datetime] = None
    id: Optional[int] = None


@dataclass
class Digest:
    """A daily digest record."""

    date: str
    podcast_name: str = "_default"
    source_summaries: dict = field(default_factory=dict)
    executive_summary: str = ""
    podcast_script: str = ""
    audio_path: str = ""
    m4a_path: str = ""
    duration_seconds: float = 0.0
    published_at: Optional[datetime] = None
    status: str = "pending"
    error_message: str = ""
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    id: Optional[int] = None


class ContentStore:
    """SQLite-based content index for the digest pipeline."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Create tables if they don't exist, and migrate if needed."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS content_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_name TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        title TEXT,
                        content TEXT NOT NULL,
                        url TEXT,
                        message_id INTEGER,
                        content_hash TEXT NOT NULL,
                        published_at TIMESTAMP,
                        collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(source_name, content_hash)
                    )
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_content_collected
                    ON content_items(collected_at)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_content_source
                    ON content_items(source_name, collected_at)
                """)

                # Check if digests table needs migration
                cursor.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='digests'"
                )
                if cursor.fetchone():
                    self._migrate_digests(conn)
                else:
                    self._create_digests_table(conn)

                # Add publish columns to existing digests table
                self._migrate_digests_publish_columns(conn)

                # Create LLM cache table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS llm_cache (
                        cache_key TEXT PRIMARY KEY,
                        stage TEXT NOT NULL,
                        output_text TEXT NOT NULL,
                        model TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.commit()
                logger.info("Content store initialized at %s", self.db_path)

        except Exception:
            logger.error("Failed to initialize content store", exc_info=True)
            raise

    @staticmethod
    def _create_digests_table(conn: sqlite3.Connection):
        """Create the digests table with podcast_name support."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                podcast_name TEXT NOT NULL DEFAULT '_default',
                source_summaries TEXT,
                executive_summary TEXT,
                podcast_script TEXT,
                audio_path TEXT,
                m4a_path TEXT,
                duration_seconds REAL,
                published_at TIMESTAMP,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                UNIQUE(date, podcast_name)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_digests_podcast
            ON digests(podcast_name, date)
        """)

    @staticmethod
    def _migrate_digests(conn: sqlite3.Connection):
        """Migrate digests table to add podcast_name if missing."""
        cursor = conn.execute("PRAGMA table_info(digests)")
        columns = {row[1] for row in cursor.fetchall()}

        if "podcast_name" in columns:
            return  # already migrated

        logger.info("Migrating digests table: adding podcast_name column")

        conn.execute("ALTER TABLE digests RENAME TO _digests_old")
        conn.execute("""
            CREATE TABLE digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                podcast_name TEXT NOT NULL DEFAULT '_default',
                source_summaries TEXT,
                executive_summary TEXT,
                podcast_script TEXT,
                audio_path TEXT,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                UNIQUE(date, podcast_name)
            )
        """)
        conn.execute("""
            INSERT INTO digests
                (id, date, podcast_name, source_summaries, executive_summary,
                 podcast_script, audio_path, status, error_message,
                 created_at, completed_at)
            SELECT
                id, date, '_default', source_summaries, executive_summary,
                podcast_script, audio_path, status, error_message,
                created_at, completed_at
            FROM _digests_old
        """)
        conn.execute("DROP TABLE _digests_old")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_digests_podcast
            ON digests(podcast_name, date)
        """)
        logger.info("Digests table migration complete")

    @staticmethod
    def _migrate_digests_publish_columns(conn: sqlite3.Connection):
        """Add publishing columns to digests table if missing."""
        cursor = conn.execute("PRAGMA table_info(digests)")
        columns = {row[1] for row in cursor.fetchall()}

        new_columns = [
            ("m4a_path", "TEXT"),
            ("duration_seconds", "REAL"),
            ("published_at", "TIMESTAMP"),
        ]
        for col_name, col_type in new_columns:
            if col_name not in columns:
                conn.execute(
                    f"ALTER TABLE digests ADD COLUMN {col_name} {col_type}"
                )
                logger.info("Added column %s to digests table", col_name)

    @staticmethod
    def _content_hash(source_name: str, content: str) -> str:
        """Generate a deduplication hash for content."""
        payload = f"{source_name}:{content}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def store_content(
        self,
        source_name: str,
        source_type: str,
        content: str,
        title: Optional[str] = None,
        url: Optional[str] = None,
        message_id: Optional[int] = None,
        published_at: Optional[datetime] = None,
    ) -> bool:
        """Store a content item, deduplicating by hash.

        Args:
            source_name: Identifier for the source (channel name, feed name).
            source_type: Either 'telegram' or 'web'.
            content: The text content.
            title: Optional title.
            url: Optional URL.
            message_id: Optional Telegram message ID.
            published_at: Optional publication timestamp.

        Returns:
            True if the item was inserted, False if it was a duplicate.
        """
        content_hash = self._content_hash(source_name, content)

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO content_items
                    (source_name, source_type, title, content, url,
                     message_id, content_hash, published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_name,
                        source_type,
                        title,
                        content,
                        url,
                        message_id,
                        content_hash,
                        published_at,
                    ),
                )
                inserted = cursor.rowcount > 0
                conn.commit()
                if inserted:
                    logger.debug(
                        "Stored content from %s: %s",
                        source_name,
                        (title or content[:60]),
                    )
                return inserted

        except Exception:
            logger.error(
                "Failed to store content from %s", source_name, exc_info=True
            )
            return False

    def get_content_since(
        self,
        since: datetime,
        source_name: Optional[str] = None,
        source_names: Optional[list[str]] = None,
    ) -> list[ContentItem]:
        """Retrieve content items collected since a given time.

        Args:
            since: Cutoff datetime.
            source_name: Optional filter by single source.
            source_names: Optional filter by list of sources.

        Returns:
            List of ContentItem objects.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                since_str = since.strftime("%Y-%m-%d %H:%M:%S")

                if source_name:
                    cursor.execute(
                        """
                        SELECT * FROM content_items
                        WHERE collected_at >= ? AND source_name = ?
                        ORDER BY published_at ASC
                        """,
                        (since_str, source_name),
                    )
                elif source_names:
                    placeholders = ",".join("?" for _ in source_names)
                    cursor.execute(
                        f"""
                        SELECT * FROM content_items
                        WHERE collected_at >= ?
                          AND source_name IN ({placeholders})
                        ORDER BY source_name, published_at ASC
                        """,
                        [since_str, *source_names],
                    )
                else:
                    cursor.execute(
                        """
                        SELECT * FROM content_items
                        WHERE collected_at >= ?
                        ORDER BY source_name, published_at ASC
                        """,
                        (since_str,),
                    )

                rows = cursor.fetchall()
                return [self._row_to_content_item(row) for row in rows]

        except Exception:
            logger.error("Failed to query content", exc_info=True)
            return []

    def create_digest(
        self,
        date: str,
        podcast_name: str = "_default",
    ) -> Digest:
        """Create or retrieve a digest record.

        Args:
            date: Date string in YYYY-MM-DD format.
            podcast_name: Podcast identifier.

        Returns:
            The Digest object.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute(
                    "INSERT OR IGNORE INTO digests (date, podcast_name) "
                    "VALUES (?, ?)",
                    (date, podcast_name),
                )
                conn.commit()

                cursor.execute(
                    "SELECT * FROM digests "
                    "WHERE date = ? AND podcast_name = ?",
                    (date, podcast_name),
                )
                row = cursor.fetchone()
                return self._row_to_digest(row)

        except Exception:
            logger.error(
                "Failed to create digest for %s/%s",
                date,
                podcast_name,
                exc_info=True,
            )
            raise

    def update_digest(
        self,
        date: str,
        podcast_name: str = "_default",
        **fields,
    ) -> None:
        """Update fields on a digest record.

        Args:
            date: Date string in YYYY-MM-DD format.
            podcast_name: Podcast identifier.
            **fields: Column name/value pairs to update.
        """
        if not fields:
            return

        # Serialize source_summaries dict to JSON
        if "source_summaries" in fields and isinstance(
            fields["source_summaries"], dict
        ):
            fields["source_summaries"] = json.dumps(
                fields["source_summaries"], ensure_ascii=False
            )

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [date, podcast_name]

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"UPDATE digests SET {set_clause} "  # noqa: S608
                    f"WHERE date = ? AND podcast_name = ?",
                    values,
                )
                conn.commit()

        except Exception:
            logger.error(
                "Failed to update digest for %s/%s",
                date,
                podcast_name,
                exc_info=True,
            )
            raise

    def get_digest(
        self,
        date: str,
        podcast_name: str = "_default",
    ) -> Optional[Digest]:
        """Retrieve a digest record.

        Args:
            date: Date string in YYYY-MM-DD format.
            podcast_name: Podcast identifier.

        Returns:
            Digest object or None.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM digests "
                    "WHERE date = ? AND podcast_name = ?",
                    (date, podcast_name),
                )
                row = cursor.fetchone()
                return self._row_to_digest(row) if row else None

        except Exception:
            logger.error(
                "Failed to get digest for %s/%s",
                date,
                podcast_name,
                exc_info=True,
            )
            return None

    def list_digests(
        self,
        limit: int = 10,
        podcast_name: Optional[str] = None,
    ) -> list[Digest]:
        """List recent digests.

        Args:
            limit: Maximum number of digests to return.
            podcast_name: Optional filter by podcast. Shows all if None.

        Returns:
            List of Digest objects, most recent first.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                if podcast_name:
                    cursor.execute(
                        "SELECT * FROM digests "
                        "WHERE podcast_name = ? "
                        "ORDER BY date DESC LIMIT ?",
                        (podcast_name, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM digests "
                        "ORDER BY date DESC LIMIT ?",
                        (limit,),
                    )
                return [self._row_to_digest(row) for row in cursor.fetchall()]

        except Exception:
            logger.error("Failed to list digests", exc_info=True)
            return []

    def get_source_names(
        self,
        since: datetime,
        source_filter: Optional[list[str]] = None,
    ) -> list[str]:
        """Get distinct source names with content since a given time.

        Args:
            since: Cutoff datetime.
            source_filter: Optional list of source names to restrict to.

        Returns:
            List of source name strings.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                since_str = since.strftime("%Y-%m-%d %H:%M:%S")

                if source_filter:
                    placeholders = ",".join("?" for _ in source_filter)
                    cursor.execute(
                        f"""
                        SELECT DISTINCT source_name FROM content_items
                        WHERE collected_at >= ?
                          AND source_name IN ({placeholders})
                        ORDER BY source_name
                        """,
                        [since_str, *source_filter],
                    )
                else:
                    cursor.execute(
                        """
                        SELECT DISTINCT source_name FROM content_items
                        WHERE collected_at >= ?
                        ORDER BY source_name
                        """,
                        (since_str,),
                    )
                return [row[0] for row in cursor.fetchall()]

        except Exception:
            logger.error("Failed to get source names", exc_info=True)
            return []

    def get_llm_cache(self, cache_key: str) -> Optional[str]:
        """Retrieve a cached LLM response.

        Args:
            cache_key: The cache key string.

        Returns:
            Cached output text, or None if not found.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT output_text FROM llm_cache "
                    "WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()
                return row[0] if row else None
        except Exception:
            logger.error("Failed to read LLM cache", exc_info=True)
            return None

    def set_llm_cache(
        self,
        cache_key: str,
        stage: str,
        output: str,
        model: str,
    ) -> None:
        """Store an LLM response in the cache.

        Args:
            cache_key: The cache key string.
            stage: Pipeline stage name (selection, source_summary, etc.).
            output: The LLM output text.
            model: Model identifier used.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache "
                    "(cache_key, stage, output_text, model) "
                    "VALUES (?, ?, ?, ?)",
                    (cache_key, stage, output, model),
                )
                conn.commit()
        except Exception:
            logger.error("Failed to write LLM cache", exc_info=True)

    def clear_llm_cache(self) -> int:
        """Delete all LLM cache entries.

        Returns:
            Number of rows deleted.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("DELETE FROM llm_cache")
                conn.commit()
                count = cursor.rowcount
                logger.info("Cleared %d LLM cache entries", count)
                return count
        except Exception:
            logger.error("Failed to clear LLM cache", exc_info=True)
            return 0

    def get_recent_summaries(
        self,
        podcast_name: str,
        before_date: str,
        limit: int = 3,
    ) -> list[tuple[str, str]]:
        """Return recent episode summaries for prior-context injection.

        Args:
            podcast_name: Podcast identifier.
            before_date: Only return episodes before this date (YYYY-MM-DD).
            limit: Maximum number of prior episodes.

        Returns:
            List of (date, executive_summary) tuples, most recent first.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT date, executive_summary FROM digests "
                    "WHERE podcast_name = ? AND date < ? "
                    "AND executive_summary != '' "
                    "ORDER BY date DESC LIMIT ?",
                    (podcast_name, before_date, limit),
                )
                return [(row[0], row[1]) for row in cursor.fetchall()]
        except Exception:
            logger.error(
                "Failed to get recent summaries", exc_info=True
            )
            return []

    @staticmethod
    def _row_to_content_item(row: sqlite3.Row) -> ContentItem:
        """Convert a database row to a ContentItem."""
        return ContentItem(
            id=row["id"],
            source_name=row["source_name"],
            source_type=row["source_type"],
            title=row["title"],
            content=row["content"],
            url=row["url"],
            message_id=row["message_id"],
            content_hash=row["content_hash"],
            published_at=row["published_at"],
            collected_at=row["collected_at"],
        )

    @staticmethod
    def _row_to_digest(row: sqlite3.Row) -> Digest:
        """Convert a database row to a Digest."""
        summaries_raw = row["source_summaries"]
        if summaries_raw:
            try:
                summaries = json.loads(summaries_raw)
            except (json.JSONDecodeError, TypeError):
                summaries = {}
        else:
            summaries = {}

        return Digest(
            id=row["id"],
            date=row["date"],
            podcast_name=row["podcast_name"],
            source_summaries=summaries,
            executive_summary=row["executive_summary"] or "",
            podcast_script=row["podcast_script"] or "",
            audio_path=row["audio_path"] or "",
            m4a_path=row["m4a_path"] or "",
            duration_seconds=float(row["duration_seconds"] or 0.0),
            published_at=row["published_at"],
            status=row["status"] or "pending",
            error_message=row["error_message"] or "",
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )
