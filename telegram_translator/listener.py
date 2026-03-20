from telethon import TelegramClient, events
from telethon.tl.types import InputChannel
import logging
import yaml
import html
import re
import sys
import asyncio
from pathlib import Path

# Import our configuration managers
from telegram_translator.config_manager import ConfigManager
from telegram_translator.channel_manager import ChannelManager
from telegram_translator.translation_manager import TranslationManager
from telegram_translator.persistence_manager import PersistenceManager

# Setup logging
logging.basicConfig(
    format='[%(levelname)s %(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)
logging.getLogger('telethon').setLevel(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Global variables
client = None
channel_manager = None
channel_entities = {}
persistence_managers = {}
config_manager = None
translation_manager = None
content_filter = None
max_message_length = 3980
max_video_message_length = 1024
enable_link_preview = False
parse_mode = 'html'

class ContentFilter:
    """LLM-based content filtering for channels"""
    
    def __init__(self, translation_manager: TranslationManager):
        self.translation_manager = translation_manager
    
    async def should_process_message(self, channel_name: str, message_text: str, channel_prompt: str = None) -> bool:
        """Check if a message should be processed based on LLM filtering"""
        if not channel_prompt:
            return True  # No filtering if no prompt specified
        
        try:
            # Create a filtering prompt
            filter_prompt = f"""
You are a content filter for a Telegram channel translator. Your task is to determine if a message should be translated and forwarded.

Channel: {channel_name}
Filtering criteria: {channel_prompt}

Message to evaluate:
{message_text}

Respond with ONLY "YES" if the message should be processed, or "NO" if it should be skipped.
Consider relevance, quality, and whether the content matches the filtering criteria.
"""
            
            # Use the translation manager's LLM to evaluate
            response = await self.translation_manager._call_llm(filter_prompt, max_tokens=10)
            
            # Parse response
            response = response.strip().upper()
            return response.startswith("YES")
            
        except Exception as e:
            logger.error(f"Error in content filtering: {e}")
            return True  # Default to processing if filtering fails

async def initialize_bot():
    """Initialize the bot with all components"""
    global client, channel_manager, channel_entities, persistence_managers, config_manager, translation_manager, content_filter, max_message_length, max_video_message_length, enable_link_preview, parse_mode
    
    # Initialize configuration
    try:
        config_manager = ConfigManager()
        translation_manager = TranslationManager(config_manager.get_translation_config())
    except FileNotFoundError as e:
        print(f"Configuration error: {e}")
        print("Please copy src/config.yml.example to src/config.yml and configure it.")
        print("Please copy src/channels.yml.example to src/channels.yml and configure it.")
        sys.exit(1)
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    # Get Telegram credentials
    credentials = config_manager.get_telegram_credentials()
    api_id = credentials['api_id']
    api_hash = credentials['api_hash']
    session_path = credentials['session_name']

    # Get processing configuration
    processing_config = config_manager.get_processing_config()
    max_message_length = processing_config.get('max_message_length', 3980)
    max_video_message_length = processing_config.get('max_video_message_length', 1024)
    enable_link_preview = processing_config.get('enable_link_preview', False)
    parse_mode = processing_config.get('parse_mode', 'html')
    
    # Create and connect the client
    client = TelegramClient(session_path, api_id, api_hash)
    
    try:
        await client.start()
        print('[Telethon] Client is listening...')
    except Exception as e:
        logger.error(f"Failed to start Telegram client: {e}")
        sys.exit(1)
    
    # Initialize channel manager with client for name resolution
    try:
        channel_manager = ChannelManager(client=client)
    except Exception as e:
        logger.error(f"Failed to initialize channel manager: {e}")
        sys.exit(1)
    
    # Initialize content filter
    content_filter = ContentFilter(translation_manager)
    
    # Initialize channel entities
    await initialize_channel_entities()
    
    # Initialize persistence managers
    persistence_managers = await initialize_persistence_managers()
    
    # Check for last messages from monitored channels
    await check_last_messages()
    
    # Print configuration summary
    channel_manager.print_summary()
    
    # Print resolved channel information
    await channel_manager.print_resolved_summary()
    
    # Print persistence statistics
    await print_persistence_stats()
    
    # Print app directory information
    config_manager.print_app_info()
    
    # Print translation provider info
    provider_info = translation_manager.get_provider_info()
    print(f"\n🔤 Translation Provider: {provider_info['provider']}")
    if provider_info['provider'] == 'openai':
        print(f"   Model: {provider_info['model']}")
        print(f"   Max tokens: {provider_info['max_tokens']}")
        print(f"   Temperature: {provider_info['temperature']}")
    elif provider_info['provider'] == 'idioma':
        print(f"   Source language: {provider_info['source_language']}")
        print(f"   Target language: {provider_info['target_language']}")
    
    # Log total number of input and output channels
    total_input_channels = len(channel_manager.get_all_input_channels())
    total_output_channels = len(channel_manager.get_all_output_channels())
    print(f"[Telethon] Listening to {total_input_channels} {'channel' if total_input_channels == 1 else 'channels'}.")
    print(f"[Telethon] Forwarding messages to {total_output_channels} {'channel' if total_output_channels == 1 else 'channels'}.")
    
    return client

async def initialize_channel_entities():
    """Initialize channel entities for channel pairs with name resolution"""
    global channel_entities
    
    # Get resolved channel pairs (with names converted to IDs)
    resolved_pairs = await channel_manager.get_resolved_channel_pairs()
    
    # Get all dialogs
    dialogs = list(client.iter_dialogs())
    
    # Initialize channel pairs
    for pair_name, pair_config in resolved_pairs.items():
        input_channels = pair_config.get('input_channels', [])
        channel_entities[pair_name] = []
        
        for dialog in dialogs:
            if dialog.entity.id in input_channels:
                channel_entities[pair_name].append(
                    InputChannel(dialog.entity.id, dialog.entity.access_hash)
                )

async def initialize_persistence_managers():
    """Initialize persistence managers for each channel pair"""
    global persistence_managers
    
    channel_pairs = channel_manager.get_channel_pairs()
    
    for pair_name, pair_config in channel_pairs.items():
        if pair_config.get('persistence', False):
            output_channel = pair_config.get('output_channel')
            if output_channel:
                persistence_manager = PersistenceManager(client, output_channel, config_manager)
                await persistence_manager.initialize()
                persistence_managers[pair_name] = persistence_manager
                logger.info(f"Initialized persistence manager for pair '{pair_name}'")
    
    return persistence_managers

async def print_persistence_stats():
    """Print persistence statistics for all enabled pairs"""
    if not persistence_managers:
        return
    
    print(f"\n💾 Persistence Statistics:")
    for pair_name, persistence_manager in persistence_managers.items():
        stats = await persistence_manager.get_stats()
        db_info = persistence_manager.get_database_info()
        
        print(f"   {pair_name}:")
        print(f"     Total processed: {stats['total_processed']}")
        print(f"     Today processed: {stats['today_processed']}")
        print(f"     Database: {db_info['size_mb']} MB" if db_info['exists'] else "     Database: Not created yet")
        if stats.get('oldest_message'):
            print(f"     Oldest message: {stats['oldest_message']}")
        print()

async def check_last_messages():
    """Check for the last message from each monitored channel and process if not already translated"""
    print("\n🔍 Checking for last messages from monitored channels...")
    
    channel_pairs = channel_manager.get_channel_pairs()
    
    for pair_name, pair_config in channel_pairs.items():
        input_channels = pair_config.get('input_channels', [])
        output_channel = pair_config.get('output_channel')
        
        if not input_channels or not output_channel:
            continue
        
        print(f"   Checking pair '{pair_name}'...")
        
        for channel_name in input_channels:
            try:
                # Get the last message from the channel
                messages = await client.get_messages(channel_name, limit=1)
                if not messages:
                    print(f"     No messages found in {channel_name}")
                    continue
                
                last_message = messages[0]
                message_text = last_message.message or ""
                
                # Check content filtering
                content_filter_prompt = pair_config.get('content_filter_prompt')
                if content_filter_prompt:
                    should_process = await content_filter.should_process_message(channel_name, message_text, content_filter_prompt)
                    if not should_process:
                        print(f"     Last message from {channel_name} filtered out by content filter")
                        continue
                
                # Check if persistence is enabled for this pair
                if pair_config.get('persistence', False):
                    persistence_manager = persistence_managers.get(pair_name)
                    if persistence_manager:
                        # Check if this message was already processed
                        if await persistence_manager.is_message_processed(channel_name, last_message.id, message_text):
                            print(f"     Last message from {channel_name} already processed")
                            continue
                
                # Process the last message
                print(f"     Processing last message from {channel_name}")
                await process_message_for_pair_sync(last_message, pair_name, pair_config, channel_name)
                
            except Exception as e:
                logger.error(f"Error checking last message from {channel_name}: {e}")

# Get the title or username of the input channel
def get_channel_name(chat):
    if hasattr(chat, 'title'):
        return chat.title
    else:
        return chat.username

def format_message(original_text: str, translated_text: str, chat_name: str, link: str, 
                  message_id: int, message_type: str = "text") -> str:
    """Format message for output"""
    border = '~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~'
    
    if message_type == "text":
        if translated_text:
            message = (
                f'<p><p>{border}\n'
                f'<b>{html.escape(chat_name)}</b>\n'
                f'{border}\n\n</p>'
                f'<p>[TRANSLATED MESSAGE]\n'
                f'{html.escape(translated_text)}\n\n</p>'
                f'<p>{border}\n'
                f'{link}/{message_id} ↩</p></p>') 
        else:
            message = (
                f'<p>{border}\n'
                f'<b>{html.escape(chat_name)}</b>\n'
                f'{border}\n\n'
                f'{link}/{message_id} ↩</p>') 

        # Message length limit appears to be around 3980 characters; must trim longer messages or they cannot be sent
        if len(message) >= max_message_length:
            formatting_chars_len = len(
                f'<p><p>{border}\n' + 
                f'<b>{html.escape(chat_name)}</b>\n' + 
                f'{border}\n\n</p>' + 
                f'<p>[TRANSLATED MESSAGE]\n' + 
                f'\n\n</p>' + 
                f'<p>{border}\n' + 
                f'{link}/{message_id} ↩</p></p>')
            
            # Subtract 3 for ellipsis
            desired_msg_len = max_message_length - formatting_chars_len - 3
            translated_text = f'{translated_text[0:desired_msg_len]}...'
            message = (
                f'<p><p>{border}\n'
                f'<b>{html.escape(chat_name)}</b>\n'
                f'{border}\n\n</p>'
                f'<p>[TRANSLATED MESSAGE]\n'
                f'{html.escape(translated_text)}\n\n</p>'
                f'<p>{border}\n'
                f'{link}/{message_id} ↩</p></p>') 
    
    elif message_type in ["video", "photo"]:
        # Format for media messages
        if translated_text:
            message = (
                f'<p><p>{link}/{message_id} ↩\n\n'
                f'{border}\n'
                f'<p><b>{html.escape(chat_name)}</b>\n</p>'
                f'{border}\n\n</p>'
                f'<p>[ORIGINAL MESSAGE]\n'
                f'{html.escape(original_text)}\n\n</p>'
                f'<p>[TRANSLATED MESSAGE]\n'
                f'{html.escape(translated_text)}</p></p>')
        else:
            message = (
                f'<p>{link}/{message_id} ↩\n\n' 
                f'{border}\n'
                f'<b>{html.escape(chat_name)}</b>\n'
                f'{border}</p>')

        # Handle video message length limits
        if len(message) >= max_video_message_length:
            formatting_chars_len = len(
                f'<p><p>{link}/{message_id} ↩\n\n'
                f'{border}\n'
                f'<p><b>{html.escape(chat_name)}</b>\n</p>'
                f'{border}\n\n</p>'
                f'<p>[ORIGINAL MESSAGE]\n'
                f'\n\n</p>'
                f'<p>[TRANSLATED MESSAGE]\n'
                f'</p></p>')
            
            desired_msg_len = (max_video_message_length - formatting_chars_len - 6) // 2
            translated_text = f'{translated_text[0:desired_msg_len]}...'
            original_text = f'{original_text[0:desired_msg_len]}...'
            message = (
                f'<p><p>{link}/{message_id} ↩\n\n'
                f'{border}\n'
                f'<p><b>{html.escape(chat_name)}</b>\n</p>'
                f'{border}\n\n</p>'
                f'<p>[ORIGINAL MESSAGE]\n'
                f'{html.escape(original_text)}\n\n</p>'
                f'<p>[TRANSLATED MESSAGE]\n'
                f'{html.escape(translated_text)}</p></p>')
    
    return message

async def process_message_for_pair_sync(message, pair_name: str, pair_config: dict, channel_name: str):
    """Process a message for a specific channel pair (synchronous version for initial check)"""
    # Check if channel should be excluded
    if config_manager.is_channel_excluded(channel_name):
        return
    
    # Get message content and type
    message_text = message.message or ""
    message_type = "text"
    
    if hasattr(message.media, 'document') and hasattr(message.media.document, 'mime_type'):
        if 'video' in message.media.document.mime_type:
            message_type = "video"
    elif hasattr(message.media, 'photo'):
        message_type = "photo"
    
    # Check if this media type is supported for this pair
    supported_media_types = pair_config.get('media_types', ['text'])
    if message_type not in supported_media_types:
        return
    
    # Check content filtering
    content_filter_prompt = pair_config.get('content_filter_prompt')
    if content_filter_prompt:
        should_process = await content_filter.should_process_message(channel_name, message_text, content_filter_prompt)
        if not should_process:
            logger.info(f"Message {message.id} from {channel_name} filtered out by content filter")
            return
    
    # Translate if enabled for this pair
    translated_text = ""
    if pair_config.get('translation', True) and message_text:
        try:
            translated_text = await translation_manager.translate(message_text)
        except Exception as e:
            logger.error(f"Translation error: {e}")
    
    # Get chat info
    chat_name = get_channel_name(message.chat)
    if hasattr(message.chat, 'username') and message.chat.username:
        link = f't.me/{message.chat.username}'
    else:
        link = f't.me/c/{message.chat.id}'
    
    # Format message
    formatted_message = format_message(
        message_text, translated_text, chat_name, link, message.id, message_type
    )
    
    # Send to output channel
    output_channel = pair_config.get('output_channel')
    if output_channel:
        try:
            await client.send_message(
                output_channel, 
                formatted_message, 
                parse_mode=parse_mode,
                file=message.media if message_type in ["video", "photo"] else None,
                link_preview=enable_link_preview
            )
            logger.info(f"Message sent to pair '{pair_name}' (output: {output_channel})")
            
            # Mark as processed if persistence is enabled
            if pair_config.get('persistence', False):
                persistence_manager = persistence_managers.get(pair_name)
                if persistence_manager:
                    await persistence_manager.mark_message_processed(channel_name, message.id, message_text)
            
        except Exception as exc:
            logger.error(f'Error while sending message to pair "{pair_name}": {exc}')

async def process_message_for_pair(event, pair_name: str, pair_config: dict):
    """Process a message for a specific channel pair"""
    # Check if channel should be excluded
    chat = await event.get_chat()
    channel_name = chat.username or ""
    if config_manager.is_channel_excluded(channel_name):
        return
    
    # Get message content and type
    message_text = event.message.message or ""
    message_type = "text"
    
    if hasattr(event.media, 'document') and hasattr(event.media.document, 'mime_type'):
        if 'video' in event.media.document.mime_type:
            message_type = "video"
    elif hasattr(event.media, 'photo'):
        message_type = "photo"
    
    # Check if this media type is supported for this pair
    supported_media_types = pair_config.get('media_types', ['text'])
    if message_type not in supported_media_types:
        return
    
    # Check content filtering
    content_filter_prompt = pair_config.get('content_filter_prompt')
    if content_filter_prompt:
        should_process = await content_filter.should_process_message(channel_name, message_text, content_filter_prompt)
        if not should_process:
            logger.info(f"Message {event.id} from {channel_name} filtered out by content filter")
            return
    
    # Check if persistence is enabled and message was already processed
    if pair_config.get('persistence', False):
        persistence_manager = persistence_managers.get(pair_name)
        if persistence_manager:
            if await persistence_manager.is_message_processed(channel_name, event.id, message_text):
                logger.info(f"Message {event.id} from {channel_name} already processed, skipping")
                return
    
    # Translate if enabled for this pair
    translated_text = ""
    if pair_config.get('translation', True) and message_text:
        try:
            translated_text = await translation_manager.translate(message_text)
        except Exception as e:
            logger.error(f"Translation error: {e}")
    
    # Get chat info
    chat_name = get_channel_name(chat)
    if chat.username:
        link = f't.me/{chat.username}'
    else:
        link = f't.me/c/{chat.id}'
    
    # Format message
    formatted_message = format_message(
        message_text, translated_text, chat_name, link, event.id, message_type
    )
    
    # Send to output channel
    output_channel = pair_config.get('output_channel')
    if output_channel:
        try:
            await client.send_message(
                output_channel, 
                formatted_message, 
                parse_mode=parse_mode,
                file=event.media if message_type in ["video", "photo"] else None,
                link_preview=enable_link_preview
            )
            logger.info(f"Message sent to pair '{pair_name}' (output: {output_channel})")
            
            # Mark as processed if persistence is enabled
            if pair_config.get('persistence', False):
                persistence_manager = persistence_managers.get(pair_name)
                if persistence_manager:
                    await persistence_manager.mark_message_processed(channel_name, event.id, message_text)
            
        except Exception as exc:
            logger.error(f'Error while sending message to pair "{pair_name}": {exc}')

# Setup event handlers for channel pairs
def setup_channel_pair_handlers():
    """Setup event handlers for all channel pairs"""
    channel_pairs = channel_manager.get_channel_pairs()
    
    for pair_name, pair_config in channel_pairs.items():
        input_entities = channel_entities.get(pair_name, [])
        if not input_entities:
            logger.warning(f"No input channels found for pair '{pair_name}'")
            continue
        
        # Create handler for this pair
        @client.on(events.NewMessage(chats=input_entities))
        async def handler(event, pair_name=pair_name, pair_config=pair_config):
            await process_message_for_pair(event, pair_name, pair_config)

async def main():
    """Main entry point"""
    try:
        print('[Telegram Translator] Starting bot...')
        
        # Initialize the bot
        await initialize_bot()
        
        print(f'[Telegram Translator] Session file: {session_path}')
        print(f'[Telegram Translator] Log file: {config_manager.get_log_path()}')
        
        # Setup handlers for channel pairs
        setup_channel_pair_handlers()
        
        # Run client until a keyboard interrupt (ctrl+C)
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print('\n[Telegram Translator] Shutting down...')
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())