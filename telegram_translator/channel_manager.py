import yaml
import logging
from typing import Dict, List, Any, Optional, Tuple
from telethon.tl.types import InputChannel
from telethon.errors import UsernameNotOccupiedError, ChannelPrivateError

logger = logging.getLogger(__name__)

class ChannelManager:
    """Manages channel configuration and routing"""
    
    def __init__(self, channels_file: str = "channels.yml", client=None):
        self.channels_file = channels_file
        self.client = client
        self.channels_config = self._load_channels()
        self.channel_pairs = self._parse_channel_pairs()
        self.channel_cache = {}  # Cache for name -> ID resolution
        
    def _load_channels(self) -> Dict[str, Any]:
        """Load channels configuration from YAML file"""
        try:
            with open(self.channels_file, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            logger.error(f"Channels file {self.channels_file} not found")
            raise
        except yaml.YAMLError as e:
            logger.error(f"Error parsing channels file: {e}")
            raise
    
    def _parse_channel_pairs(self) -> Dict[str, Dict[str, Any]]:
        """Parse the channel pairs format"""
        pairs = self.channels_config.get('channel_pairs', {})
        
        if not pairs:
            raise ValueError(
                "No channel pairs found in configuration. "
                "Please configure at least one channel pair in the 'channel_pairs' section."
            )
        
        # Validate channel pairs
        for pair_name, pair_config in pairs.items():
            required_fields = ['input_channels', 'output_channel']
            for field in required_fields:
                if field not in pair_config:
                    logger.warning(f"Channel pair '{pair_name}' missing required field: {field}")
            
            # Set defaults
            if 'media_types' not in pair_config:
                pair_config['media_types'] = ['text']
            if 'translation' not in pair_config:
                pair_config['translation'] = True
            if 'persistence' not in pair_config:
                pair_config['persistence'] = False
            if 'description' not in pair_config:
                pair_config['description'] = f"Channel pair: {pair_name}"
        
        return pairs
    
    async def resolve_channel_name(self, channel_name: str) -> Optional[int]:
        """Resolve channel name to channel ID"""
        if not self.client:
            logger.warning("Telegram client not available for channel name resolution")
            return None
        
        # Check cache first
        if channel_name in self.channel_cache:
            return self.channel_cache[channel_name]
        
        try:
            # Try to resolve username (remove @ if present)
            username = channel_name.lstrip('@')
            
            # Get entity by username
            entity = await self.client.get_entity(username)
            
            if hasattr(entity, 'id'):
                channel_id = entity.id
                self.channel_cache[channel_name] = channel_id
                logger.info(f"Resolved channel '{channel_name}' to ID: {channel_id}")
                return channel_id
            else:
                logger.warning(f"Entity '{channel_name}' is not a channel")
                return None
                
        except UsernameNotOccupiedError:
            logger.error(f"Channel username '{channel_name}' does not exist")
            return None
        except ChannelPrivateError:
            logger.error(f"Channel '{channel_name}' is private or you don't have access")
            return None
        except Exception as e:
            logger.error(f"Error resolving channel '{channel_name}': {e}")
            return None
    
    async def resolve_channel_pairs(self) -> Dict[str, Dict[str, Any]]:
        """Resolve all channel names to IDs in channel pairs"""
        resolved_pairs = {}
        
        for pair_name, pair_config in self.channel_pairs.items():
            resolved_config = pair_config.copy()
            
            # Resolve input channels
            input_channels = pair_config.get('input_channels', [])
            resolved_input_channels = []
            
            for channel_name in input_channels:
                resolved_id = await self.resolve_channel_name(channel_name)
                if resolved_id:
                    resolved_input_channels.append(resolved_id)
                else:
                    logger.warning(f"Could not resolve input channel '{channel_name}' in pair '{pair_name}'")
            
            resolved_config['input_channels'] = resolved_input_channels
            
            # Resolve output channel
            output_channel = pair_config.get('output_channel')
            if output_channel:
                resolved_output = await self.resolve_channel_name(output_channel)
                if resolved_output:
                    resolved_config['output_channel'] = resolved_output
                else:
                    logger.warning(f"Could not resolve output channel '{output_channel}' in pair '{pair_name}'")
                    resolved_config['output_channel'] = None
            
            resolved_pairs[pair_name] = resolved_config
        
        return resolved_pairs
    
    def get_channel_pairs(self) -> Dict[str, Dict[str, Any]]:
        """Get all configured channel pairs (with unresolved names)"""
        return self.channel_pairs
    
    async def get_resolved_channel_pairs(self) -> Dict[str, Dict[str, Any]]:
        """Get channel pairs with all names resolved to IDs"""
        return await self.resolve_channel_pairs()
    
    def get_channel_metadata(self) -> Dict[str, str]:
        """Get channel metadata/descriptions"""
        return self.channels_config.get('channel_metadata', {})
    
    def get_input_channels_for_pair(self, pair_name: str) -> List[str]:
        """Get input channel names for a specific pair"""
        pair = self.channel_pairs.get(pair_name)
        if pair:
            return pair.get('input_channels', [])
        return []
    
    def get_output_channel_for_pair(self, pair_name: str) -> Optional[str]:
        """Get output channel name for a specific pair"""
        pair = self.channel_pairs.get(pair_name)
        if pair:
            return pair.get('output_channel')
        return None
    
    def get_media_types_for_pair(self, pair_name: str) -> List[str]:
        """Get media types for a specific pair"""
        pair = self.channel_pairs.get(pair_name)
        if pair:
            return pair.get('media_types', ['text'])
        return ['text']
    
    def should_translate_for_pair(self, pair_name: str) -> bool:
        """Check if translation is enabled for a specific pair"""
        pair = self.channel_pairs.get(pair_name)
        if pair:
            return pair.get('translation', True)
        return True
    
    def should_persist_for_pair(self, pair_name: str) -> bool:
        """Check if persistence is enabled for a specific pair"""
        pair = self.channel_pairs.get(pair_name)
        if pair:
            return pair.get('persistence', False)
        return False
    
    def get_pair_description(self, pair_name: str) -> str:
        """Get description for a specific pair"""
        pair = self.channel_pairs.get(pair_name)
        if pair:
            return pair.get('description', f"Channel pair: {pair_name}")
        return f"Channel pair: {pair_name}"
    
    def get_all_input_channels(self) -> List[str]:
        """Get all input channel names from all pairs"""
        all_channels = set()
        
        for pair in self.channel_pairs.values():
            all_channels.update(pair.get('input_channels', []))
        
        return list(all_channels)
    
    def get_all_output_channels(self) -> List[str]:
        """Get all output channel names"""
        all_channels = set()
        
        for pair in self.channel_pairs.values():
            output_channel = pair.get('output_channel')
            if output_channel:
                all_channels.add(output_channel)
        
        return list(all_channels)
    
    def find_pair_for_input_channel(self, channel_name: str) -> List[Tuple[str, Dict[str, Any]]]:
        """Find all pairs that include a specific input channel"""
        matching_pairs = []
        
        for pair_name, pair_config in self.channel_pairs.items():
            if channel_name in pair_config.get('input_channels', []):
                matching_pairs.append((pair_name, pair_config))
        
        return matching_pairs
    
    def validate_configuration(self) -> List[str]:
        """Validate channel configuration and return any errors"""
        errors = []
        
        # Check channel pairs
        for pair_name, pair_config in self.channel_pairs.items():
            if not pair_config.get('input_channels'):
                errors.append(f"Channel pair '{pair_name}' has no input channels")
            
            if not pair_config.get('output_channel'):
                errors.append(f"Channel pair '{pair_name}' has no output channel")
            
            # Validate media types
            valid_media_types = ['text', 'photo', 'video']
            media_types = pair_config.get('media_types', [])
            for media_type in media_types:
                if media_type not in valid_media_types:
                    errors.append(f"Channel pair '{pair_name}' has invalid media type: {media_type}")
        
        # Check for duplicate output channels
        output_channels = []
        for pair_config in self.channel_pairs.values():
            output_channel = pair_config.get('output_channel')
            if output_channel:
                if output_channel in output_channels:
                    errors.append(f"Duplicate output channel: {output_channel}")
                output_channels.append(output_channel)
        
        return errors
    
    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the channel configuration"""
        total_input_channels = len(self.get_all_input_channels())
        total_output_channels = len(self.get_all_output_channels())
        total_pairs = len(self.channel_pairs)
        
        pair_summary = {}
        for pair_name, pair_config in self.channel_pairs.items():
            pair_summary[pair_name] = {
                'input_count': len(pair_config.get('input_channels', [])),
                'output_channel': pair_config.get('output_channel'),
                'media_types': pair_config.get('media_types', []),
                'translation': pair_config.get('translation', True),
                'persistence': pair_config.get('persistence', False),
                'description': pair_config.get('description', '')
            }
        
        return {
            'total_input_channels': total_input_channels,
            'total_output_channels': total_output_channels,
            'total_pairs': total_pairs,
            'pairs': pair_summary,
            'errors': self.validate_configuration()
        }
    
    def print_summary(self):
        """Print a human-readable summary of the configuration"""
        summary = self.get_summary()
        
        print(f"\n📊 Channel Configuration Summary:")
        print(f"   Total input channels: {summary['total_input_channels']}")
        print(f"   Total output channels: {summary['total_output_channels']}")
        print(f"   Channel pairs: {summary['total_pairs']}")
        
        if summary['errors']:
            print(f"\n⚠️  Configuration Errors:")
            for error in summary['errors']:
                print(f"   - {error}")
        else:
            print(f"\n✅ Configuration is valid")
        
        if summary['pairs']:
            print(f"\n📋 Channel Pairs:")
            for pair_name, pair_info in summary['pairs'].items():
                print(f"   {pair_name}:")
                print(f"     Input channels: {pair_info['input_count']}")
                print(f"     Output channel: {pair_info['output_channel']}")
                print(f"     Media types: {', '.join(pair_info['media_types'])}")
                print(f"     Translation: {'Yes' if pair_info['translation'] else 'No'}")
                print(f"     Persistence: {'Yes' if pair_info['persistence'] else 'No'}")
                if pair_info['description']:
                    print(f"     Description: {pair_info['description']}")
                print()
    
    async def print_resolved_summary(self):
        """Print a summary with resolved channel IDs"""
        resolved_pairs = await self.get_resolved_channel_pairs()
        
        print(f"\n🔍 Resolved Channel Configuration:")
        for pair_name, pair_config in resolved_pairs.items():
            print(f"   {pair_name}:")
            print(f"     Input channels: {pair_config.get('input_channels', [])}")
            print(f"     Output channel: {pair_config.get('output_channel')}")
            print() 