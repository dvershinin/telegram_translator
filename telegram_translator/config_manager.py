import yaml
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from appdirs import user_data_dir, user_config_dir

logger = logging.getLogger(__name__)

class ConfigManager:
    """Manages configuration for the Telegram Translator app"""
    
    def __init__(self, config_file: str = "config.yml"):
        self.config_file = config_file
        self.config = self._load_config()
        
        # Initialize app directories using appdirs
        self.app_name = "telegram_translator"
        self.app_author = "telegram_translator"
        
        # Get proper cross-platform paths
        self.data_dir = Path(user_data_dir(self.app_name, self.app_author))
        self.config_dir = Path(user_config_dir(self.app_name, self.app_author))
        self.sessions_dir = self.data_dir / "sessions"
        self.logs_dir = self.data_dir / "logs"
        self.databases_dir = self.data_dir / "databases"
        self.podcasts_dir = Path("./podcasts")
        
        # Create directories if they don't exist
        self._ensure_directories()
        
    def _ensure_directories(self):
        """Ensure all required directories exist"""
        directories = [
            self.data_dir,
            self.config_dir,
            self.sessions_dir,
            self.logs_dir,
            self.databases_dir,
            self.podcasts_dir,
        ]
        
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Ensured directory exists: {directory}")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing configuration file: {e}")
    
    def get_telegram_credentials(self) -> Dict[str, Any]:
        """Get Telegram API credentials"""
        # Try environment variables first
        api_id = os.getenv('TTR_API_ID')
        api_hash = os.getenv('TTR_API_HASH')
        session_name = os.getenv('TTR_SESSION_NAME', 'telegram_translator_session')
        
        # Fall back to config file - check both root level and telegram section
        if not api_id or not api_hash:
            # First try root level (new format)
            api_id = api_id or self.config.get('api_id')
            api_hash = api_hash or self.config.get('api_hash')
            session_name = session_name or self.config.get('session_name', 'telegram_translator_session')
            
            # Then try telegram section (old format)
            if not api_id or not api_hash:
                telegram_config = self.config.get('telegram', {})
                api_id = api_id or telegram_config.get('api_id')
                api_hash = api_hash or telegram_config.get('api_hash')
                session_name = session_name or telegram_config.get('session_name', 'telegram_translator_session')
        
        if not api_id or not api_hash:
            raise ValueError("Telegram API credentials not found. Please set TTR_API_ID and TTR_API_HASH environment variables or configure them in config.yml")
        
        # Use proper session path
        session_path = self.sessions_dir / session_name
        
        return {
            'api_id': int(api_id),
            'api_hash': api_hash,
            'session_name': str(session_path)
        }
    
    def get_translation_config(self) -> Dict[str, Any]:
        """Get translation configuration"""
        translation_config = self.config.get('translation', {})
        
        # Handle environment variable substitution
        if translation_config.get('provider') == 'openai':
            openai_config = translation_config.get('openai', {})
            api_key = os.getenv('OPENAI_API_KEY') or openai_config.get('api_key')
            if api_key:
                openai_config['api_key'] = api_key
            translation_config['openai'] = openai_config
        
        return translation_config
    
    def get_processing_config(self) -> Dict[str, Any]:
        """Get message processing configuration"""
        return self.config.get('processing', {})
    
    def get_logging_config(self) -> Dict[str, Any]:
        """Get logging configuration"""
        logging_config = self.config.get('logging', {})
        
        # Use proper log file path
        log_file = logging_config.get('file', 'telegram_translator.log')
        if not Path(log_file).is_absolute():
            log_file = self.logs_dir / log_file
        
        logging_config['file'] = str(log_file)
        return logging_config
    
    def get_log_path(self) -> str:
        """Get the log file path"""
        logging_config = self.get_logging_config()
        return logging_config.get('file', str(self.logs_dir / 'telegram_translator.log'))
    
    def get_excluded_channels(self) -> list:
        """Get list of excluded channels"""
        return self.config.get('excluded_channels', [])
    
    def is_channel_excluded(self, channel_name: str) -> bool:
        """Check if a channel is excluded"""
        excluded_channels = self.get_excluded_channels()
        return channel_name in excluded_channels
    
    def get_database_path(self, database_name: str = "persistence.db") -> str:
        """Get the database file path"""
        return str(self.databases_dir / database_name)
    
    def get_app_directories(self) -> Dict[str, str]:
        """Get all app directories for debugging/info"""
        return {
            'data_dir': str(self.data_dir),
            'config_dir': str(self.config_dir),
            'sessions_dir': str(self.sessions_dir),
            'logs_dir': str(self.logs_dir),
            'databases_dir': str(self.databases_dir),
            'podcasts_dir': str(self.podcasts_dir),
        }
    
    def resolve_podcast_configs(self) -> Dict[str, Dict[str, Any]]:
        """Resolve all podcast configurations.

        If a ``podcasts`` section exists in config, each entry is resolved
        against the global ``sources`` pool.  Otherwise, a single
        ``_default`` podcast is synthesised from the legacy flat
        ``digest`` + ``podcast`` sections.

        Returns:
            Dict mapping podcast name to its fully resolved config.
        """
        podcasts_raw = self.config.get("podcasts")
        if podcasts_raw:
            return {
                name: self._resolve_single_podcast(name, cfg)
                for name, cfg in podcasts_raw.items()
            }

        # Legacy fallback: synthesise _default from flat sections
        return {"_default": self._build_legacy_podcast_config()}

    def get_podcast_config(self, name: str) -> Dict[str, Any]:
        """Return the resolved config for a single podcast.

        Args:
            name: Podcast name as defined in config.

        Returns:
            Resolved podcast config dict.

        Raises:
            ValueError: If the podcast name is not found.
        """
        all_podcasts = self.resolve_podcast_configs()
        if name not in all_podcasts:
            available = list(all_podcasts.keys())
            raise ValueError(
                f"Unknown podcast '{name}'. Available: {available}"
            )
        return all_podcasts[name]

    def _resolve_single_podcast(
        self, name: str, cfg: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Resolve a single podcast entry against the global source pool.

        Args:
            name: Podcast key name.
            cfg: Raw podcast config dict from YAML.

        Returns:
            Fully resolved podcast config.
        """
        global_sources = self.config.get("sources", {})
        global_telegram = global_sources.get("telegram", {})
        global_web = global_sources.get("web", {})
        voicebox_cfg = self.config.get("voicebox", {})

        # Resolve source references
        source_refs = cfg.get("sources", [])
        resolved_telegram = {}
        resolved_web = {}
        for ref in source_refs:
            if ref in global_telegram:
                resolved_telegram[ref] = global_telegram[ref]
            elif ref in global_web:
                resolved_web[ref] = global_web[ref]
            else:
                logger.warning(
                    "Podcast '%s' references unknown source '%s'",
                    name, ref,
                )

        default_assets = str(
            Path(__file__).resolve().parent.parent / "podcasts" / "assets"
        )
        audio_cfg = cfg.get("audio", {})

        return {
            "name": name,
            "title": cfg.get("title", name),
            "host_name": cfg.get("host_name", ""),
            "sources": {
                "telegram": resolved_telegram,
                "web": resolved_web,
            },
            "source_names": list(source_refs),
            "selection_prompt": cfg.get("selection_prompt", ""),
            "voice_profile": cfg.get("voice_profile", "default"),
            "language": cfg.get("language", "en"),
            "model": cfg.get("model", "gpt-4o"),
            "executive_prompt": cfg.get("executive_prompt", ""),
            "podcast_prompt": cfg.get("podcast_prompt", ""),
            "voicebox_url": cfg.get(
                "voicebox_url",
                voicebox_cfg.get("url", "http://localhost:17493"),
            ),
            "audio": {
                "intro_bed": audio_cfg.get(
                    "intro_bed",
                    str(Path(default_assets) / "news_bed.wav"),
                ),
                "background_bed": audio_cfg.get(
                    "background_bed",
                    str(Path(default_assets) / "background_bed.mp3"),
                ),
                "whoosh": audio_cfg.get(
                    "whoosh",
                    str(Path(default_assets) / "whoosh.wav"),
                ),
                "lead_in_seconds": float(
                    audio_cfg.get("lead_in_seconds", 4.0)
                ),
                "intro_fade_seconds": float(
                    audio_cfg.get("intro_fade_seconds", 2.0)
                ),
                "intro_bed_volume": float(
                    audio_cfg.get("intro_bed_volume", 0.7)
                ),
                "background_bed_volume": float(
                    audio_cfg.get("background_bed_volume", 0.08)
                ),
                "background_fade_seconds": float(
                    audio_cfg.get("background_fade_seconds", 3.0)
                ),
            },
            "output_dir": cfg.get("output_dir", f"./podcasts/{name}"),
            "pause_between_segments_ms": int(
                cfg.get("pause_between_segments_ms", 800)
            ),
        }

    def _build_legacy_podcast_config(self) -> Dict[str, Any]:
        """Build a _default podcast config from legacy flat sections.

        Returns:
            Resolved podcast config dict.
        """
        digest_cfg = self.config.get("digest", {})
        podcast_cfg = self.config.get("podcast", {})
        voicebox_cfg = self.config.get("voicebox", {})

        default_assets = str(
            Path(__file__).resolve().parent.parent / "podcasts" / "assets"
        )

        # In legacy mode, all sources are included
        all_sources = self.config.get("sources", {})
        all_source_names = list(all_sources.get("telegram", {}).keys()) + \
            list(all_sources.get("web", {}).keys())

        return {
            "name": "_default",
            "title": "Daily Digest",
            "host_name": "",
            "sources": all_sources,
            "source_names": all_source_names,
            "selection_prompt": "",
            "voice_profile": podcast_cfg.get("voice_profile", "default"),
            "language": podcast_cfg.get("language", "en"),
            "model": digest_cfg.get("model", "gpt-4o"),
            "executive_prompt": digest_cfg.get("executive_prompt", ""),
            "podcast_prompt": digest_cfg.get("podcast_prompt", ""),
            "voicebox_url": podcast_cfg.get(
                "voicebox_url",
                voicebox_cfg.get("url", "http://localhost:17493"),
            ),
            "audio": {
                "intro_bed": str(Path(default_assets) / "news_bed.wav"),
                "background_bed": str(
                    Path(default_assets) / "background_bed.mp3"
                ),
                "whoosh": str(Path(default_assets) / "whoosh.wav"),
                "lead_in_seconds": 4.0,
                "intro_fade_seconds": 2.0,
                "intro_bed_volume": 0.7,
                "background_bed_volume": 0.08,
                "background_fade_seconds": 3.0,
            },
            "output_dir": podcast_cfg.get("output_dir", "./podcasts"),
            "pause_between_segments_ms": int(
                podcast_cfg.get("pause_between_segments_ms", 800)
            ),
        }

    def print_app_info(self):
        """Print application directory information"""
        print(f"\n📁 Application Directories:")
        directories = self.get_app_directories()
        for name, path in directories.items():
            print(f"   {name}: {path}")
        
        # Check if directories exist
        print(f"\n📂 Directory Status:")
        for name, path in directories.items():
            path_obj = Path(path)
            if path_obj.exists():
                print(f"   ✅ {name}: {path}")
            else:
                print(f"   ❌ {name}: {path} (does not exist)") 