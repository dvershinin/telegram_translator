import logging
import hashlib
import sqlite3
import json
from typing import Dict, Set, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path
from telethon import TelegramClient

logger = logging.getLogger(__name__)

class PersistenceManager:
    """Manages message persistence using local SQLite database"""
    
    def __init__(self, client: TelegramClient, output_channel: str, config_manager=None):
        self.client = client
        self.output_channel = output_channel
        self.config_manager = config_manager
        
        # Use config manager for database path if available, otherwise fall back to local path
        if config_manager:
            self.db_path = Path(config_manager.get_database_path("persistence.db"))
        else:
            # Fallback to local data directory
            self.db_path = Path("data/persistence.db")
            self.db_path.parent.mkdir(exist_ok=True)
        
        self._init_database()
        
    def _init_database(self):
        """Initialize the SQLite database with required tables"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Create processed messages table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS processed_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_username TEXT NOT NULL,
                        message_id INTEGER NOT NULL,
                        message_hash TEXT NOT NULL,
                        message_text TEXT,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        output_channel TEXT NOT NULL,
                        UNIQUE(channel_username, message_id, output_channel)
                    )
                ''')
                
                # Create index for faster lookups
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_channel_message 
                    ON processed_messages(channel_username, message_id, output_channel)
                ''')
                
                # Create index for cleanup operations
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_processed_at 
                    ON processed_messages(processed_at)
                ''')
                
                conn.commit()
                logger.info(f"Database initialized at {self.db_path}")
                
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise
    
    def _generate_message_hash(self, channel_username: str, message_id: int, message_text: str = "") -> str:
        """Generate a unique hash for a message"""
        content = f"{channel_username}:{message_id}:{message_text}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    async def is_message_processed(self, channel_username: str, message_id: int, message_text: str = "") -> bool:
        """Check if a message has been processed before"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Check by message ID first (faster)
                cursor.execute('''
                    SELECT COUNT(*) FROM processed_messages 
                    WHERE channel_username = ? AND message_id = ? AND output_channel = ?
                ''', (channel_username, message_id, self.output_channel))
                
                if cursor.fetchone()[0] > 0:
                    return True
                
                # If not found by ID, check by hash (for content-based deduplication)
                message_hash = self._generate_message_hash(channel_username, message_id, message_text)
                cursor.execute('''
                    SELECT COUNT(*) FROM processed_messages 
                    WHERE message_hash = ? AND output_channel = ?
                ''', (message_hash, self.output_channel))
                
                return cursor.fetchone()[0] > 0
                
        except Exception as e:
            logger.error(f"Error checking message persistence: {e}")
            return False
    
    async def mark_message_processed(self, channel_username: str, message_id: int, message_text: str = ""):
        """Mark a message as processed"""
        try:
            message_hash = self._generate_message_hash(channel_username, message_id, message_text)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT OR IGNORE INTO processed_messages 
                    (channel_username, message_id, message_hash, message_text, output_channel)
                    VALUES (?, ?, ?, ?, ?)
                ''', (channel_username, message_id, message_hash, message_text, self.output_channel))
                
                conn.commit()
                
                if cursor.rowcount > 0:
                    logger.debug(f"Marked message {message_id} from {channel_username} as processed")
                
        except Exception as e:
            logger.error(f"Error marking message as processed: {e}")
    
    async def cleanup_old_messages(self, days_to_keep: int = 7):
        """Clean up old processed messages to prevent database bloat"""
        try:
            cutoff_date = datetime.now() - timedelta(days=days_to_keep)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get count before deletion
                cursor.execute('''
                    SELECT COUNT(*) FROM processed_messages 
                    WHERE processed_at < ?
                ''', (cutoff_date,))
                
                count_to_delete = cursor.fetchone()[0]
                
                if count_to_delete > 0:
                    # Delete old messages
                    cursor.execute('''
                        DELETE FROM processed_messages 
                        WHERE processed_at < ?
                    ''', (cutoff_date,))
                    
                    conn.commit()
                    logger.info(f"Cleaned up {count_to_delete} old processed messages")
                
        except Exception as e:
            logger.error(f"Error cleaning up old messages: {e}")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get persistence statistics"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Total processed messages
                cursor.execute('''
                    SELECT COUNT(*) FROM processed_messages 
                    WHERE output_channel = ?
                ''', (self.output_channel,))
                total_processed = cursor.fetchone()[0]
                
                # Messages processed today
                today = datetime.now().date()
                cursor.execute('''
                    SELECT COUNT(*) FROM processed_messages 
                    WHERE output_channel = ? AND DATE(processed_at) = ?
                ''', (self.output_channel, today))
                today_processed = cursor.fetchone()[0]
                
                # Oldest message
                cursor.execute('''
                    SELECT MIN(processed_at) FROM processed_messages 
                    WHERE output_channel = ?
                ''', (self.output_channel,))
                oldest_message = cursor.fetchone()[0]
                
                return {
                    "total_processed": total_processed,
                    "today_processed": today_processed,
                    "output_channel": self.output_channel,
                    "oldest_message": oldest_message,
                    "database_path": str(self.db_path)
                }
                
        except Exception as e:
            logger.error(f"Error getting persistence stats: {e}")
            return {
                "total_processed": 0,
                "today_processed": 0,
                "output_channel": self.output_channel,
                "error": str(e)
            }
    
    async def reset_persistence(self):
        """Reset all processed messages for this output channel (use with caution)"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    DELETE FROM processed_messages 
                    WHERE output_channel = ?
                ''', (self.output_channel,))
                
                conn.commit()
                logger.warning(f"Reset persistence data for channel {self.output_channel}")
                
        except Exception as e:
            logger.error(f"Error resetting persistence: {e}")
    
    async def get_recent_messages(self, limit: int = 10) -> list:
        """Get recent processed messages for debugging"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT channel_username, message_id, message_text, processed_at 
                    FROM processed_messages 
                    WHERE output_channel = ? 
                    ORDER BY processed_at DESC 
                    LIMIT ?
                ''', (self.output_channel, limit))
                
                return cursor.fetchall()
                
        except Exception as e:
            logger.error(f"Error getting recent messages: {e}")
            return []
    
    async def initialize(self):
        """Initialize the persistence manager"""
        logger.info(f"Initializing persistence manager for {self.output_channel}")
        
        # Perform initial cleanup
        await self.cleanup_old_messages()
        
        # Log initial stats
        stats = await self.get_stats()
        logger.info(f"Persistence manager initialized. Total processed: {stats['total_processed']}")
    
    def get_database_info(self) -> Dict[str, Any]:
        """Get database file information"""
        try:
            if self.db_path.exists():
                stat = self.db_path.stat()
                return {
                    "exists": True,
                    "size_bytes": stat.st_size,
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "path": str(self.db_path)
                }
            else:
                return {
                    "exists": False,
                    "path": str(self.db_path)
                }
        except Exception as e:
            return {
                "exists": False,
                "error": str(e),
                "path": str(self.db_path)
            } 