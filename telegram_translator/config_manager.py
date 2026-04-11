import yaml
import os
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from appdirs import user_data_dir, user_config_dir

logger = logging.getLogger(__name__)

# Keys that a destination-scoped podcast must NOT set in its per-podcast
# ``publish:`` block — they come from the destination.
_FORBIDDEN_PODCAST_PUBLISH_KEYS = ("base_url", "publish_dir", "sync_command")

_VALID_DESTINATION_TYPES = ("static", "astro_collection")

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
    
    def resolve_destinations(self) -> Dict[str, Dict[str, Any]]:
        """Resolve all publish destinations.

        Parses the top-level ``destinations:`` section and normalizes each
        entry: defaults ``type`` to ``static``, strips trailing slashes from
        ``base_url``, and expands ``~`` in directory paths.

        Returns:
            Dict mapping destination name to its resolved config dict.

        Raises:
            ValueError: If a destination has an unknown ``type`` or is
                missing required keys for its type.
        """
        raw = self.config.get("destinations") or {}
        resolved: Dict[str, Dict[str, Any]] = {}

        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                raise ValueError(
                    f"Destination '{name}' must be a mapping, "
                    f"got {type(cfg).__name__}"
                )

            dest_type = cfg.get("type", "static")
            if dest_type not in _VALID_DESTINATION_TYPES:
                raise ValueError(
                    f"Destination '{name}' has invalid type "
                    f"'{dest_type}'. Must be one of: "
                    f"{', '.join(_VALID_DESTINATION_TYPES)}"
                )

            base_url = str(cfg.get("base_url", "")).rstrip("/")
            sync_command = cfg.get("sync_command", "")

            entry: Dict[str, Any] = {
                "name": name,
                "type": dest_type,
                "base_url": base_url,
                "sync_command": sync_command,
            }

            if dest_type == "static":
                publish_dir = cfg.get("publish_dir")
                if not publish_dir:
                    raise ValueError(
                        f"Static destination '{name}' requires "
                        f"'publish_dir'"
                    )
                entry["publish_dir"] = str(
                    Path(publish_dir).expanduser()
                )
                entry["site_title"] = cfg.get("site_title", "")
                entry["site_description"] = cfg.get(
                    "site_description", ""
                )
                entry["copyright"] = cfg.get("copyright", "")
            else:  # astro_collection
                content_dir = cfg.get("content_dir")
                public_dir = cfg.get("public_dir")
                if not content_dir or not public_dir:
                    raise ValueError(
                        f"astro_collection destination '{name}' "
                        f"requires both 'content_dir' and 'public_dir'"
                    )
                entry["content_dir"] = str(
                    Path(content_dir).expanduser()
                )
                entry["public_dir"] = str(
                    Path(public_dir).expanduser()
                )

            resolved[name] = entry

        return resolved

    def resolve_podcast_configs(self) -> Dict[str, Dict[str, Any]]:
        """Resolve all podcast configurations.

        If a ``podcasts`` section exists in config, each entry is resolved
        against the global ``sources`` pool and any referenced destination.
        Otherwise, a single ``_default`` podcast is synthesised from the
        legacy flat ``digest`` + ``podcast`` sections.

        Returns:
            Dict mapping podcast name to its fully resolved config.

        Raises:
            ValueError: If any destination reference or slug layout is
                invalid.
        """
        podcasts_raw = self.config.get("podcasts")
        if not podcasts_raw:
            # Legacy fallback: synthesise _default from flat sections
            return {"_default": self._build_legacy_podcast_config()}

        destinations = self.resolve_destinations()
        resolved = {
            name: self._resolve_single_podcast(name, cfg, destinations)
            for name, cfg in podcasts_raw.items()
        }

        self._validate_destination_grouping(resolved, destinations)
        return resolved

    def group_podcasts_by_destination(
        self,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group destination-scoped podcasts by destination name.

        Returns:
            Dict mapping destination name to the list of resolved podcast
            config dicts assigned to that destination. Legacy podcasts
            without a ``destination_name`` are excluded.
        """
        all_podcasts = self.resolve_podcast_configs()
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for cfg in all_podcasts.values():
            dest_name = cfg.get("destination_name")
            if not dest_name:
                continue
            groups.setdefault(dest_name, []).append(cfg)
        return groups

    def _validate_destination_grouping(
        self,
        resolved: Dict[str, Dict[str, Any]],
        destinations: Dict[str, Dict[str, Any]],
    ) -> None:
        """Validate destination grouping invariants.

        Ensures that:
        - Each ``astro_collection`` destination has at most one podcast.
        - Each ``static`` destination either has exactly one root-mounted
          podcast (slug ``""``) or 1+ subpath podcasts, never a mix.

        Args:
            resolved: All resolved podcast configs keyed by podcast name.
            destinations: All resolved destination configs keyed by name.

        Raises:
            ValueError: If any invariant is violated.
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for cfg in resolved.values():
            dest_name = cfg.get("destination_name")
            if dest_name:
                groups.setdefault(dest_name, []).append(cfg)

        for dest_name, podcasts in groups.items():
            dest_type = destinations[dest_name]["type"]

            if dest_type == "astro_collection" and len(podcasts) > 1:
                names = ", ".join(p["name"] for p in podcasts)
                raise ValueError(
                    f"astro_collection destination '{dest_name}' can "
                    f"host only one podcast, got {len(podcasts)}: "
                    f"{names}"
                )

            if dest_type == "static":
                root = [p for p in podcasts if p.get("slug") == ""]
                sub = [p for p in podcasts if p.get("slug")]
                if root and sub:
                    root_names = ", ".join(p["name"] for p in root)
                    sub_names = ", ".join(p["name"] for p in sub)
                    raise ValueError(
                        f"Static destination '{dest_name}' cannot mix "
                        f"root-mounted and subpath podcasts. "
                        f"Root-mounted: {root_names}. "
                        f"Subpath: {sub_names}."
                    )
                if len(root) > 1:
                    names = ", ".join(p["name"] for p in root)
                    raise ValueError(
                        f"Static destination '{dest_name}' has "
                        f"multiple root-mounted podcasts "
                        f"(slug=''): {names}"
                    )

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
        self,
        name: str,
        cfg: Dict[str, Any],
        destinations: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Resolve a single podcast entry against the global source pool.

        If the raw config references a destination, merge destination-level
        keys (``base_url``, ``publish_dir`` / ``content_dir`` / ``public_dir``,
        ``sync_command``) into the resolved ``publish`` dict according to the
        destination's ``type``.

        Args:
            name: Podcast key name.
            cfg: Raw podcast config dict from YAML.
            destinations: Resolved destinations map (from
                ``resolve_destinations``). If omitted, destination references
                are ignored.

        Returns:
            Fully resolved podcast config.

        Raises:
            ValueError: If the podcast references an unknown destination or
                its ``publish:`` block contains forbidden destination-level
                keys.
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

        publish_dict, destination_name, destination_type, slug = (
            self._resolve_publish_section(name, cfg, destinations)
        )

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
            "api_base": cfg.get("api_base"),
            "api_key_env": cfg.get("api_key_env"),
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
            "publish": publish_dict,
            "destination_name": destination_name,
            "destination_type": destination_type,
            "slug": slug,
        }

    def _resolve_publish_section(
        self,
        name: str,
        cfg: Dict[str, Any],
        destinations: Optional[Dict[str, Dict[str, Any]]],
    ) -> tuple[Dict[str, Any], Optional[str], Optional[str], Optional[str]]:
        """Resolve the publish dict for a podcast, honoring destination refs.

        Args:
            name: Podcast key name.
            cfg: Raw podcast config dict.
            destinations: Resolved destinations map, or None to skip
                destination handling.

        Returns:
            A 4-tuple ``(publish_dict, destination_name, destination_type,
            slug)``. ``destination_name``, ``destination_type``, and ``slug``
            are ``None`` for legacy podcasts without a ``destination:`` key.

        Raises:
            ValueError: If the podcast references an unknown destination or
                its ``publish:`` block contains forbidden destination-level
                keys.
        """
        raw_publish = dict(cfg.get("publish") or {})
        dest_ref = cfg.get("destination")

        if not dest_ref:
            return raw_publish, None, None, None

        if destinations is None or dest_ref not in destinations:
            available = list(destinations.keys()) if destinations else []
            raise ValueError(
                f"Podcast '{name}' references unknown destination "
                f"'{dest_ref}'. Available: {available}"
            )

        # Forbid destination-level keys inside the podcast publish block
        forbidden_present = [
            k for k in _FORBIDDEN_PODCAST_PUBLISH_KEYS if k in raw_publish
        ]
        if forbidden_present:
            raise ValueError(
                f"Podcast '{name}' is destination-scoped "
                f"(destination='{dest_ref}') and must not set "
                f"{forbidden_present} inside its 'publish:' block — "
                f"these are provided by the destination."
            )

        dest = destinations[dest_ref]
        dest_type = dest["type"]
        base_url = dest["base_url"]

        if dest_type == "static":
            slug_raw = cfg.get("slug")
            slug = name if slug_raw is None else str(slug_raw)
            if slug:
                derived_base_url = f"{base_url}/{slug}"
                derived_publish_dir = str(
                    Path(dest["publish_dir"]) / slug
                )
            else:
                derived_base_url = base_url
                derived_publish_dir = dest["publish_dir"]

            publish = dict(raw_publish)
            publish["base_url"] = derived_base_url
            publish["publish_dir"] = derived_publish_dir
            publish["sync_command"] = dest.get("sync_command", "")
            return publish, dest_ref, dest_type, slug

        # astro_collection
        publish = dict(raw_publish)
        publish["base_url"] = base_url
        publish["content_dir"] = dest["content_dir"]
        publish["public_dir"] = dest["public_dir"]
        publish["sync_command"] = dest.get("sync_command", "")
        return publish, dest_ref, dest_type, None

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
            "publish": {},
            "destination_name": None,
            "destination_type": None,
            "slug": None,
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