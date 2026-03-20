# Telegram Translator App

---

## Introduction

An app that retrieves messages from the Channels that an account is subscribed to on Telegram, translates them into English, and sends the translation (and the untranslated, source message if you choose) to a new Channel of your choice in near real-time.

The reason this app was developed was so that I could have a way to follow Russian and Ukrainian-language Telegram posts to keep up-to-date with the war in Ukraine without needing to manually translate every post. In addition, the video listener component was developed to help with the Telehunt project of @benborges.

To do this, the app utilizes:

- the telethon package to interact with the Telegram API,
- OpenAI GPT models or Idioma for translation (configurable),
- flexible channel pair configuration for multiple input-output mappings,
- channel names (usernames) for easy configuration,
- smart persistence mechanism using local SQLite database to avoid duplicate translations,
- proper cross-platform path handling using appdirs for data storage,
- **CLI interface for easy bot management and interaction**,
- **LLM-based content filtering to skip irrelevant posts**,
- **Interactive bot control via Saved Messages**

It is my hope that this will help others to engage with the stories of those who are actually on the ground in Ukraine and Russia as well as the moment-to-moment beats of this unspeakably tragic war.

## Requirements

- **Python 3.12+** (recommended)
- Python 3.11+ (minimum)
- Virtual environment (recommended)
- OpenAI API key (for OpenAI translation) or Idioma (for free translation)

## Installation

1. Clone the code in this repo.
2. Create and activate a virtual environment:
   ```bash
   # Using venv (recommended)
   python3.12 -m venv venv
   source venv/bin/activate  # On macOS/Linux
   # or
   venv\Scripts\activate     # On Windows
   
   # Or using virtualenvwrapper
   mkvirtualenv telegram_translator -p python3.12
   workon telegram_translator
   ```

3. Install the project in editable mode:
   ```bash
   pip install -e .
   ```

## Configuration

### Option 1: Environment Variables (Recommended)

1. Copy the environment example file:
   ```bash
   cp env.example .env
   ```

2. Edit `.env` and add your credentials:
   ```bash
   TTR_API_ID=your_api_id_here
   TTR_API_HASH=your_api_hash_here
   OPENAI_API_KEY=your_openai_api_key_here  # Required for OpenAI translation
   ```

### Option 2: Configuration File

1. Copy the configuration example:
   ```bash
   cp config.yml.example config.yml
   ```

2. Edit `config.yml` and add your credentials:
   ```yaml
   api_id: your_api_id_here
   api_hash: your_api_hash_here
   translation:
     provider: "openai"  # or "idioma"
     openai:
       api_key: your_openai_api_key_here
       model: "gpt-4o-mini"
   ```

### Getting API Credentials

1. **Telegram API**: Create a Telegram account and obtain API credentials from [Telegram API website](https://core.telegram.org/api)
2. **OpenAI API**: Get an API key from [OpenAI Platform](https://platform.openai.com/api-keys) (required for OpenAI translation)

## Channel Configuration

The bot uses a flexible channel pairs system that allows you to define multiple input-output channel mappings with custom settings. **Only channel names (usernames) are supported** for easy configuration.

1. Copy the channels example file:
   ```bash
   cp channels.yml.example channels.yml
   ```

2. Edit `channels.yml` and configure your channel pairs:
   ```yaml
   channel_pairs:
     # Live monitoring: naebnet -> TestGetPageSpeed
     naebnet_monitor:
       input_channels:
         - "naebnet"
       output_channel: "TestGetPageSpeed"
       description: "Monitor naebnet channel and translate to TestGetPageSpeed"
       media_types: ["text", "photo", "video"]
       translation: true
       persistence: true  # Enable message persistence to avoid duplicates
       content_filter_prompt: |
         Only process messages that contain:
         - News about Russia, Ukraine, or international relations
         - Military or political developments
         - Important announcements or breaking news
         - Skip: advertisements, personal posts, spam, or irrelevant content
         
     # Example additional pairs
     war_news:
       input_channels:
         - "ukraine_now"
         - "russian_news"
       output_channel: "my_war_news_channel"
       description: "Main war news aggregation"
       media_types: ["text", "photo", "video"]
       translation: true
       persistence: true
       content_filter_prompt: |
         Only process messages that contain:
         - War-related news and updates
         - Military developments and analysis
         - Humanitarian or political developments
         - Skip: entertainment, sports, or unrelated content
   ```

### Channel Pair Configuration Options

- **input_channels**: List of input channel usernames to monitor
- **output_channel**: Single output channel username where messages will be sent
- **description**: Human-readable description of the channel pair
- **media_types**: List of media types to process (`text`, `photo`, `video`)
- **translation**: Whether to translate messages (default: `true`)
- **persistence**: Whether to track processed messages to avoid duplicates (default: `false`)
- **content_filter_prompt**: LLM prompt for filtering relevant content (optional)

### Content Filtering

The `content_filter_prompt` field allows you to specify criteria for which messages should be processed. The LLM will evaluate each message against this prompt and only process messages that meet the criteria. This helps avoid translating irrelevant content like advertisements, spam, or off-topic posts.

**Example content filtering prompts:**

```yaml
# For news channels
content_filter_prompt: |
  Only process messages that contain:
  - Breaking news or important announcements
  - Political or military developments
  - Significant events or updates
  - Skip: advertisements, personal posts, spam, or entertainment content

# For technical channels
content_filter_prompt: |
  Only process messages that contain:
  - Technical tutorials or guides
  - Code examples or programming tips
  - Software updates or releases
  - Skip: off-topic discussions, memes, or unrelated content
```

### How to Configure Channels

1. Find the channel's username (the part after `@` in the channel URL)
2. Add it to the `input_channels` list (without the `@` symbol)
3. Example: For channel `@naebnet`, use `"naebnet"`

### Channel Metadata (Optional)

You can add descriptions for your channels:

```yaml
channel_metadata:
  naebnet: "Russian news and updates channel"
  TestGetPageSpeed: "Test output channel for translated content"
```

## CLI Interface

The bot includes a comprehensive CLI interface for easy management and interaction.

### Available Commands

```bash
# Start the bot
telegram-translator start

# Show bot configuration and status
telegram-translator status

# List all configured channels
telegram-translator list-channels

# Validate channel configuration
telegram-translator validate
```

### Interactive Bot Control

Once the bot is running, you can interact with it through **Saved Messages** in Telegram:

- `/help` - Show available commands
- `/status` - Show bot status and monitored channels
- `/stats` - Show translation and persistence statistics
- `/channels` - List all configured channel pairs
- `/filter` - Show content filtering prompts
- `/stop` - Stop the bot

### Example CLI Usage

```bash
# Check bot status
$ telegram-translator status
📊 Bot Configuration Status:
========================================

📁 Application Directories:
   data_dir: /Users/danila/Library/Application Support/telegram_translator
   config_dir: /Users/danila/Library/Application Support/telegram_translator
   sessions_dir: /Users/danila/Library/Application Support/telegram_translator/sessions
   logs_dir: /Users/danila/Library/Application Support/telegram_translator/logs
   databases_dir: /Users/danila/Library/Application Support/telegram_translator/databases

📊 Channel Configuration Summary:
   Total input channels: 1
   Total output channels: 1
   Channel pairs: 1

✅ Configuration is valid

🔤 Translation Provider: idioma

# List configured channels
$ telegram-translator list-channels
📋 Configured Channel Pairs:
========================================

🔗 naebnet_monitor:
  Description: Monitor naebnet channel and translate to TestGetPageSpeed
  Input channels: naebnet
  Output channel: TestGetPageSpeed
  Media types: text, photo, video
  Translation: Yes
  Persistence: Yes
  Content filter: Only process messages that contain:
- News about R...
```

## Data Storage

The app uses **proper cross-platform paths** for data storage using the `appdirs` package:

### Storage Locations

| Platform | Data Directory | Config Directory |
|----------|----------------|------------------|
| **macOS** | `~/Library/Application Support/telegram_translator/` | `~/Library/Application Support/telegram_translator/` |
| **Linux** | `~/.local/share/telegram_translator/` | `~/.config/telegram_translator/` |
| **Windows** | `%APPDATA%\telegram_translator\` | `%APPDATA%\telegram_translator\` |

### Directory Structure

```
telegram_translator/
├── sessions/          # Telegram session files
├── logs/             # Application log files
└── databases/        # SQLite database files
    └── persistence.db # Message persistence database
```

### Benefits

- **Cross-platform compatibility**: Works consistently across macOS, Linux, and Windows
- **Proper permissions**: Uses system-standard locations with correct permissions
- **No clutter**: Doesn't create files in the project directory
- **User isolation**: Each user has their own data directory
- **Easy backup**: All data is in standard system locations

## Smart Persistence System

The bot includes a smart persistence mechanism that prevents duplicate translations using a **local SQLite database**:

### How It Works

1. **Local SQLite Database**: Uses a local database file in the proper system data directory
2. **Automatic Deduplication**: Checks if a message was already processed before translating
3. **Initial Message Check**: When the bot starts, it checks the last message from each monitored channel
4. **Hash-Based Tracking**: Creates unique hashes for each message to ensure accurate tracking
5. **Automatic Cleanup**: Old message records are automatically cleaned up to prevent database bloat

### Persistence Features

- **No Duplicates**: Messages are only translated once, even across bot restarts
- **Private Storage**: All persistence data is stored locally, not visible in Telegram channels
- **Automatic Cleanup**: Old message records are automatically cleaned up (default: 7 days)
- **Fast Lookups**: Optimized database indexes for quick message checking
- **Configurable**: Can be enabled/disabled per channel pair
- **Statistics**: Provides detailed statistics about processed messages

### Database Structure

The SQLite database contains a `processed_messages` table with:
- `channel_username`: Source channel name
- `message_id`: Telegram message ID
- `message_hash`: Unique hash for content-based deduplication
- `message_text`: Original message text (for debugging)
- `processed_at`: Timestamp when message was processed
- `output_channel`: Target output channel

### Example Database Statistics

```
💾 Persistence Statistics:
   naebnet_monitor:
     Total processed: 156
     Today processed: 12
     Database: 0.05 MB
     Oldest message: 2024-01-08T10:30:00
```

## Translation Providers

The bot supports two translation providers:

### OpenAI (Default)

Uses GPT models for high-quality translation with customizable prompts.

```yaml
translation:
  provider: "openai"
  openai:
    api_key: ${OPENAI_API_KEY}
    model: "gpt-4o-mini"  # "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"
    max_tokens: 1000
    temperature: 0.3
    system_prompt: |
      You are a professional translator specializing in Russian and Ukrainian to English translation.
      Your task is to translate messages accurately while preserving the original meaning and tone.
      For war-related content, maintain sensitivity and accuracy.
      Always respond with only the translated text, nothing else.
```

### Idioma (Free Alternative)

Uses the Idioma library for free translation.

```yaml
translation:
  provider: "idioma"
  idioma:
    source_language: "auto"  # auto-detect source language
    target_language: "en"    # translate to English
```

## Usage

### Starting the Bot

1. **Using CLI (Recommended)**:
   ```bash
   telegram-translator start
   ```

2. **Direct Python execution**:
   ```bash
   # From the project root
   python -m telegram_translator.listener
   ```

### Authentication

- On the first launch, you will be prompted for the phone number associated with your account by the Telegram authorization API.
- After entering it, the Telegram API will send you a confirmation code to your Telegram account.
- Input that code and press enter.
- If your account has 2FA enabled, you will be prompted for your password.
- Once authenticated, a `.session` file will be created in the proper system data directory.
- As long as this file is present, you will not need to re-authenticate when you launch the program again.

### Interactive Control

Once the bot is running, you can control it through **Saved Messages** in Telegram:

1. Open your **Saved Messages** in Telegram
2. Send commands like `/status`, `/stats`, `/channels`, etc.
3. The bot will respond with the requested information

## Configuration Options

The bot supports various configuration options through `src/config.yml`:

### Session Management
- **session_name**: Name for the session file (default: "telegram_translator_session")
- **session_storage_dir**: Automatically managed by appdirs

### Translation Settings
- **provider**: Translation provider ("openai" or "idioma")
- **openai**: OpenAI-specific settings (model, max_tokens, temperature, system_prompt)
- **idioma**: Idioma-specific settings (source_language, target_language)

### Logging
- **level**: Logging level (DEBUG, INFO, WARNING, ERROR)
- **file**: Log file path (automatically managed by appdirs)

### Message Processing
- **max_message_length**: Maximum message length (default: 3980)
- **max_video_message_length**: Maximum video message length (default: 1024)
- **enable_link_preview**: Enable link previews (default: false)
- **parse_mode**: Message parsing mode (default: "html")

### Channel Filtering
- **excluded_channels**: List of channel usernames to exclude from processing

## File Structure

```
telegram_translator/
├── telegram_translator/        # Main package directory
│   ├── __init__.py            # Package initialization
│   ├── cli.py                 # CLI interface for bot management
│   ├── listener.py            # Main bot application
│   ├── config_manager.py      # Configuration management (with appdirs)
│   ├── channel_manager.py     # Channel configuration management
│   ├── translation_manager.py # Translation provider management
│   └── persistence_manager.py # Message persistence management (SQLite)
├── config.yml.example         # Configuration template
├── channels.yml.example       # Channel configuration template
├── config.yml                 # Live configuration
├── channels.yml               # Live channel configuration
├── requirements.txt           # Python dependencies
├── pyproject.toml            # Project metadata and Python version
├── setup.py                  # Package installation script
├── env.example               # Environment variables template
└── README.md                 # This file

# System data directories (managed by appdirs):
# macOS: ~/Library/Application Support/telegram_translator/
# Linux: ~/.local/share/telegram_translator/
# Windows: %APPDATA%\telegram_translator\
```

## Development

### Setting up Development Environment

1. Install development dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

2. Run code formatting:
   ```bash
   black src/
   ```

3. Run type checking:
   ```bash
   mypy src/
   ```

4. Run tests:
   ```bash
   pytest
   ```

### Python Version Compatibility

This project is designed for **Python 3.12** but supports Python 3.11+. The `pyproject.toml` file specifies the minimum Python version requirement.

## Troubleshooting

### Common Issues

1. **Configuration not found**: Ensure you've copied `config.yml.example` to `config.yml`
2. **Channels not found**: Ensure you've copied `channels.yml.example` to `channels.yml`
3. **Session authentication**: Delete the session file in the system data directory to re-authenticate
4. **Translation errors**: Check your internet connection and API key validity
5. **Channel not found**: Ensure the bot account has access to the specified channels
6. **No messages being processed**: Check that input channels are correctly configured and the bot has access to them
7. **OpenAI API errors**: Verify your OpenAI API key and account balance
8. **Channel name resolution failed**: Check that the channel username is correct and the bot has access to it
9. **Database errors**: Check file permissions for the system data directory
10. **CLI not working**: Ensure the package is installed with `pip install -e .`
11. **Content filtering not working**: Check that the LLM provider is properly configured

### Logs

Check the log file in the system data directory for detailed error information.

### Channel Configuration Validation

The bot will automatically validate your channel configuration and show a summary when starting:

```
📊 Channel Configuration Summary:
   Total input channels: 1
   Total output channels: 1
   Channel pairs: 1

✅ Configuration is valid

📋 Channel Pairs:
   naebnet_monitor:
     Input channels: 1
     Output channel: TestGetPageSpeed
     Media types: text, photo, video
     Translation: Yes
     Persistence: Yes
     Description: Monitor naebnet channel and translate to TestGetPageSpeed

🔍 Resolved Channel Configuration:
   naebnet_monitor:
     Input channels: [1234567890]
     Output channel: 9876543210

💾 Persistence Statistics:
   naebnet_monitor:
     Total processed: 0
     Today processed: 0
     Database: Not created yet

📁 Application Directories:
   data_dir: /Users/danila/Library/Application Support/telegram_translator
   config_dir: /Users/danila/Library/Application Support/telegram_translator
   sessions_dir: /Users/danila/Library/Application Support/telegram_translator/sessions
   logs_dir: /Users/danila/Library/Application Support/telegram_translator/logs
   databases_dir: /Users/danila/Library/Application Support/telegram_translator/databases

📂 Directory Status:
   ✅ data_dir: /Users/danila/Library/Application Support/telegram_translator
   ✅ config_dir: /Users/danila/Library/Application Support/telegram_translator
   ✅ sessions_dir: /Users/danila/Library/Application Support/telegram_translator/sessions
   ✅ logs_dir: /Users/danila/Library/Application Support/telegram_translator/logs
   ✅ databases_dir: /Users/danila/Library/Application Support/telegram_translator/databases

🔤 Translation Provider: openai
   Model: gpt-4o-mini
   Max tokens: 1000
   Temperature: 0.3

🔍 Checking for last messages from monitored channels...
   Checking pair 'naebnet_monitor'...
     Processing last message from naebnet
```

## Pending Features

- Export and parsing capabilities
- Support for additional translation providers
- Web interface for configuration management
- Advanced content filtering and moderation
- Real-time configuration updates
- Backup and restore functionality
