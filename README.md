# Telegram Gemini Bot

A Telegram-based remote controller for managing and interacting with local projects and AI sessions via the Telegram Bot API.

## Features

- **Project Management**: Switch between different local projects and sessions.
- **Remote Execution**: Run commands and interact with sessions remotely.
- **Dynamic Interface**: Uses Telegram keyboards and inline buttons for easy navigation.
- **Security**: Restricted to a single authorized user (via `TELEGRAM_USER` ID).

## Configuration

The bot requires the following environment variables to be set:

- `TELEGRAM_TOKEN`: Your official Telegram Bot API token (from @BotFather).
- `TELEGRAM_USER`: Your numerical Telegram User ID (to restrict access).
- `BOT_START_DIR`: (Optional) The base directory where the bot should operate.

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/jpwilh/telegram-gemini.git
   cd telegram-gemini
   ```

2. **Setup virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Set environment variables**:
   Add them to your `.bashrc` or a `.env` file (not included in the repo for security):
   ```bash
   export TELEGRAM_TOKEN="your_token_here"
   export TELEGRAM_USER="your_id_here"
   ```

## Usage

Start the bot using the provided shell script:
```bash
./start_bot.sh
```

Once started, use the Telegram interface to:
- List available projects.
- Select an active workspace.
- Interact with the running session.

## License

MIT (or your preferred license)
