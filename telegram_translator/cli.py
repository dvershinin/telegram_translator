#!/usr/bin/env python3
"""
CLI interface for Telegram Translator Bot
"""

import click
import asyncio
import logging
import sys
from pathlib import Path
from typing import Dict, Any, Optional

# Import our modules
from telegram_translator.config_manager import ConfigManager
from telegram_translator.channel_manager import ChannelManager
from telegram_translator.translation_manager import TranslationManager
from telegram_translator.persistence_manager import PersistenceManager
from telegram_translator.listener import initialize_bot, setup_channel_pair_handlers, process_message_for_pair_sync, initialize_persistence_managers
from telethon import events

# Setup logging
logging.basicConfig(
    format='[%(levelname)s %(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)
logging.getLogger('telethon').setLevel(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Global variables for CLI
client = None
channel_manager = None
persistence_managers = {}
config_manager = None
translation_manager = None

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

async def handle_saved_messages(event):
    """Handle messages sent to Saved Messages for bot interaction"""
    global client, channel_manager, persistence_managers, config_manager, translation_manager
    
    message_text = event.message.message.lower().strip()
    
    if message_text == "/help":
        help_text = """
🤖 Telegram Translator Bot Commands:

/status - Show bot status and monitored channels
/stats - Show translation and persistence statistics
/channels - List all configured channel pairs
/filter - Show content filtering prompts
/restart - Restart the bot
/stop - Stop the bot

For more help, visit the project repository.
"""
        await client.send_message('me', help_text)
    
    elif message_text == "/status":
        status_text = "🤖 Bot Status:\n\n"
        
        # Show monitored channels
        channel_pairs = channel_manager.get_channel_pairs()
        status_text += "📡 Monitored Channels:\n"
        for pair_name, pair_config in channel_pairs.items():
            input_channels = pair_config.get('input_channels', [])
            output_channel = pair_config.get('output_channel')
            status_text += f"  {pair_name}:\n"
            status_text += f"    Input: {', '.join(input_channels)}\n"
            status_text += f"    Output: {output_channel}\n"
            status_text += f"    Translation: {'Yes' if pair_config.get('translation') else 'No'}\n"
            status_text += f"    Persistence: {'Yes' if pair_config.get('persistence') else 'No'}\n\n"
        
        # Show translation provider
        provider_info = translation_manager.get_provider_info()
        status_text += f"🔤 Translation Provider: {provider_info['provider']}\n"
        if provider_info['provider'] == 'openai':
            status_text += f"  Model: {provider_info['model']}\n"
        
        await client.send_message('me', status_text)
    
    elif message_text == "/stats":
        stats_text = "📊 Bot Statistics:\n\n"
        
        # Show persistence statistics
        if persistence_managers:
            stats_text += "💾 Persistence Statistics:\n"
            for pair_name, persistence_manager in persistence_managers.items():
                stats = await persistence_manager.get_stats()
                db_info = persistence_manager.get_database_info()
                stats_text += f"  {pair_name}:\n"
                stats_text += f"    Total processed: {stats['total_processed']}\n"
                stats_text += f"    Today processed: {stats['today_processed']}\n"
                if db_info['exists']:
                    stats_text += f"    Database size: {db_info['size_mb']} MB\n"
                stats_text += "\n"
        else:
            stats_text += "💾 No persistence managers active\n\n"
        
        await client.send_message('me', stats_text)
    
    elif message_text == "/channels":
        channels_text = "📋 Channel Configuration:\n\n"
        
        channel_pairs = channel_manager.get_channel_pairs()
        for pair_name, pair_config in channel_pairs.items():
            channels_text += f"🔗 {pair_name}:\n"
            channels_text += f"  Description: {pair_config.get('description', 'No description')}\n"
            channels_text += f"  Input channels: {', '.join(pair_config.get('input_channels', []))}\n"
            channels_text += f"  Output channel: {pair_config.get('output_channel')}\n"
            channels_text += f"  Media types: {', '.join(pair_config.get('media_types', []))}\n"
            channels_text += f"  Translation: {'Yes' if pair_config.get('translation') else 'No'}\n"
            channels_text += f"  Persistence: {'Yes' if pair_config.get('persistence') else 'No'}\n"
            
            # Show content filtering prompt if available
            if pair_config.get('content_filter_prompt'):
                channels_text += f"  Content filter: {pair_config['content_filter_prompt'][:50]}...\n"
            
            channels_text += "\n"
        
        await client.send_message('me', channels_text)
    
    elif message_text == "/filter":
        filter_text = "🔍 Content Filtering Prompts:\n\n"
        
        channel_pairs = channel_manager.get_channel_pairs()
        for pair_name, pair_config in channel_pairs.items():
            filter_prompt = pair_config.get('content_filter_prompt')
            if filter_prompt:
                filter_text += f"📝 {pair_name}:\n"
                filter_text += f"  {filter_prompt}\n\n"
            else:
                filter_text += f"📝 {pair_name}: No filtering prompt set\n\n"
        
        await client.send_message('me', filter_text)
    
    elif message_text == "/restart":
        await client.send_message('me', "🔄 Restarting bot...")
        # This would require more complex restart logic
        await client.send_message('me', "⚠️ Restart functionality not yet implemented")
    
    elif message_text == "/stop":
        await client.send_message('me', "🛑 Stopping bot...")
        await client.disconnect()
        sys.exit(0)
    
    elif message_text.startswith("/"):
        await client.send_message('me', f"❓ Unknown command: {message_text}\nUse /help for available commands.")

async def run_bot():
    """Run the Telegram Translator bot with CLI features"""
    global client, channel_manager, persistence_managers, config_manager, translation_manager
    
    try:
        # Initialize configuration
        config_manager = ConfigManager()
        translation_manager = TranslationManager(config_manager.get_translation_config())
        
        # Initialize the bot
        client = await initialize_bot()
        
        # Initialize channel manager with client for name resolution
        channel_manager = ChannelManager(client=client)
        
        # Initialize persistence managers
        persistence_managers = await initialize_persistence_managers()
        
        # Setup event handlers for channel pairs
        setup_channel_pair_handlers()
        
        # Setup Saved Messages handler for CLI interaction
        @client.on(events.NewMessage(chats='me'))
        async def saved_messages_handler(event):
            await handle_saved_messages(event)
        
        # Send startup message to Saved Messages
        startup_message = """
🤖 Telegram Translator Bot Started!

Available commands (send to Saved Messages):
/help - Show this help message
/status - Show bot status and monitored channels
/stats - Show translation and persistence statistics
/channels - List all configured channel pairs
/filter - Show content filtering prompts
/stop - Stop the bot

Bot is now monitoring channels and ready to translate!
"""
        await client.send_message('me', startup_message)
        
        # Run client until disconnected
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        if client:
            await client.send_message('me', f"❌ Bot error: {e}")
        sys.exit(1)

@click.group()
def cli():
    """Telegram Translator Bot CLI"""
    pass

@cli.command()
def start():
    """Start the Telegram Translator bot"""
    click.echo("🚀 Starting Telegram Translator Bot...")
    asyncio.run(run_bot())

@cli.command()
def status():
    """Show bot configuration and status"""
    try:
        config_manager = ConfigManager()
        channel_manager = ChannelManager("channels.yml")
        
        click.echo("📊 Bot Configuration Status:")
        click.echo("=" * 40)
        
        # Show app directories
        config_manager.print_app_info()
        
        # Show channel configuration
        channel_manager.print_summary()
        
        # Show translation provider
        translation_manager = TranslationManager(config_manager.get_translation_config())
        provider_info = translation_manager.get_provider_info()
        click.echo(f"\n🔤 Translation Provider: {provider_info['provider']}")
        if provider_info['provider'] == 'openai':
            click.echo(f"   Model: {provider_info['model']}")
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)

@cli.command()
@click.option('--config-file', default='channels.yml', help='Path to channels configuration file')
def validate(config_file):
    """Validate channel configuration"""
    try:
        channel_manager = ChannelManager(config_file)
        errors = channel_manager.validate_configuration()
        
        if errors:
            click.echo("❌ Configuration errors found:")
            for error in errors:
                click.echo(f"  - {error}")
        else:
            click.echo("✅ Configuration is valid")
            channel_manager.print_summary()
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)

@cli.command()
@click.option('--config-file', default='channels.yml', help='Path to channels configuration file')
def list_channels(config_file):
    """List all configured channels"""
    try:
        channel_manager = ChannelManager(config_file)
        channel_pairs = channel_manager.get_channel_pairs()
        
        click.echo("📋 Configured Channel Pairs:")
        click.echo("=" * 40)
        
        for pair_name, pair_config in channel_pairs.items():
            click.echo(f"\n🔗 {pair_name}:")
            click.echo(f"  Description: {pair_config.get('description', 'No description')}")
            click.echo(f"  Input channels: {', '.join(pair_config.get('input_channels', []))}")
            click.echo(f"  Output channel: {pair_config.get('output_channel')}")
            click.echo(f"  Media types: {', '.join(pair_config.get('media_types', []))}")
            click.echo(f"  Translation: {'Yes' if pair_config.get('translation') else 'No'}")
            click.echo(f"  Persistence: {'Yes' if pair_config.get('persistence') else 'No'}")
            
            # Show content filtering prompt if available
            if pair_config.get('content_filter_prompt'):
                click.echo(f"  Content filter: {pair_config['content_filter_prompt'][:50]}...")
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)

@cli.group()
def digest():
    """Daily news digest and podcast generation."""
    pass


@digest.command(name="run")
@click.option("--date", default=None, help="Target date (YYYY-MM-DD), defaults to today")
@click.option("--podcast", "podcast_name", default=None, help="Podcast name (runs all if omitted)")
def digest_run(date, podcast_name):
    """Run the full digest pipeline: collect, summarize, podcast."""
    from telegram_translator.digest import DigestPipeline

    async def _run():
        config_mgr = ConfigManager()
        pipeline = DigestPipeline(config_mgr, podcast_name=podcast_name)
        results = await pipeline.run(date)

        for pname, result in results.items():
            click.echo(f"Digest complete: {pname} ({date or 'today'})")
            for source, summary in result.get("source_summaries", {}).items():
                click.echo(f"  {source}: {len(summary)} chars")
            if result.get("executive_summary"):
                click.echo(f"  Executive summary: {len(result['executive_summary'])} chars")
            if result.get("audio_path"):
                click.echo(f"  Audio: {result['audio_path']}")

    asyncio.run(_run())


@digest.command(name="collect")
@click.option("--date", default=None, help="Target date (YYYY-MM-DD), defaults to today")
@click.option("--podcast", "podcast_name", default=None, help="Podcast name (collects all sources if omitted)")
def digest_collect(date, podcast_name):
    """Collect content from Telegram channels and web sources."""
    from telegram_translator.digest import DigestPipeline

    async def _run():
        config_mgr = ConfigManager()
        pipeline = DigestPipeline(config_mgr, podcast_name=podcast_name)
        count = await pipeline.collect(date)
        click.echo(f"Collected {count} new items")

    asyncio.run(_run())


@digest.command(name="summarize")
@click.option("--date", default=None, help="Target date (YYYY-MM-DD), defaults to today")
@click.option("--podcast", "podcast_name", default=None, help="Podcast name (summarizes all if omitted)")
def digest_summarize(date, podcast_name):
    """Generate summaries and podcast script from collected content."""
    from telegram_translator.digest import DigestPipeline

    async def _run():
        config_mgr = ConfigManager()
        pipeline = DigestPipeline(config_mgr, podcast_name=podcast_name)
        results = await pipeline.summarize(date)

        for pname, result in results.items():
            click.echo(f"Summarization complete: {pname} ({date or 'today'})")
            for source, summary in result.get("source_summaries", {}).items():
                click.echo(f"  {source}: {len(summary)} chars")
            if result.get("podcast_script"):
                click.echo(f"  Podcast script: {len(result['podcast_script'])} chars")

    asyncio.run(_run())


@digest.command(name="podcast")
@click.option("--date", default=None, help="Target date (YYYY-MM-DD), defaults to today")
@click.option("--podcast", "podcast_name", default=None, help="Podcast name (generates all if omitted)")
def digest_podcast(date, podcast_name):
    """Generate podcast audio from an existing script."""
    from telegram_translator.digest import DigestPipeline

    async def _run():
        config_mgr = ConfigManager()
        pipeline = DigestPipeline(config_mgr, podcast_name=podcast_name)
        results = await pipeline.podcast(date)
        for pname, audio_path in results.items():
            click.echo(f"Podcast generated: {pname} -> {audio_path}")

    asyncio.run(_run())


@digest.command(name="status")
@click.option("--date", default=None, help="Target date (YYYY-MM-DD), defaults to today")
@click.option("--podcast", "podcast_name", default=None, help="Podcast name (shows all if omitted)")
def digest_status(date, podcast_name):
    """Show the status of a digest."""
    from datetime import datetime as dt, timezone
    from telegram_translator.content_store import ContentStore

    config_mgr = ConfigManager()
    db_path = config_mgr.get_database_path("content_store.db")
    store = ContentStore(db_path)

    target_date = date or dt.now(tz=timezone.utc).strftime("%Y-%m-%d")

    if podcast_name:
        digests = [store.get_digest(target_date, podcast_name)]
        digests = [d for d in digests if d]
    else:
        digests = [
            d for d in store.list_digests(100)
            if d.date == target_date
        ]

    if not digests:
        click.echo(f"No digests found for {target_date}")
        return

    for d in digests:
        click.echo(f"Digest: {d.podcast_name} ({d.date})")
        click.echo(f"  Status: {d.status}")
        click.echo(f"  Sources summarized: {len(d.source_summaries)}")
        if d.source_summaries:
            for name in d.source_summaries:
                click.echo(f"    - {name}")
        click.echo(f"  Executive summary: {'yes' if d.executive_summary else 'no'}")
        click.echo(f"  Podcast script: {'yes' if d.podcast_script else 'no'}")
        click.echo(f"  Audio: {d.audio_path or 'not generated'}")
        if d.error_message:
            click.echo(f"  Error: {d.error_message}")


@digest.command(name="list")
@click.option("--limit", default=10, help="Number of recent digests to show")
@click.option("--podcast", "podcast_name", default=None, help="Filter by podcast name")
def digest_list(limit, podcast_name):
    """List recent digests."""
    from telegram_translator.content_store import ContentStore

    config_mgr = ConfigManager()
    db_path = config_mgr.get_database_path("content_store.db")
    store = ContentStore(db_path)

    digests = store.list_digests(limit, podcast_name=podcast_name)

    if not digests:
        click.echo("No digests found")
        return

    click.echo(f"{'Date':<12} {'Podcast':<20} {'Status':<12} {'Sources':<8} {'Audio'}")
    click.echo("-" * 65)
    for d in digests:
        audio = "yes" if d.audio_path else "no"
        click.echo(
            f"{d.date:<12} {d.podcast_name:<20} {d.status:<12} "
            f"{len(d.source_summaries):<8} {audio}"
        )


@digest.command(name="podcasts")
def digest_podcasts():
    """List configured podcasts."""
    config_mgr = ConfigManager()
    podcasts = config_mgr.resolve_podcast_configs()

    if not podcasts:
        click.echo("No podcasts configured")
        return

    for name, cfg in podcasts.items():
        title = cfg.get("title", name)
        sources = cfg.get("source_names", [])
        voice = cfg.get("voice_profile", "?")
        click.echo(f"  {name}: {title}")
        click.echo(f"    Voice: {voice}, Sources: {', '.join(sources)}")


if __name__ == '__main__':
    cli() 