# Telegram Gemini Bot

A powerful Telegram-based interface for interacting with the Gemini CLI remotely. This bot allows you to manage multiple projects, maintain isolated sessions per Telegram topic, and execute AI-powered tasks through your mobile device or desktop Telegram client.

## Features

- **Project Management**: Switch between multiple local project directories.
- **Topic Isolation**: Each Telegram topic has its own active project and isolated `.gemini` environment (including chat history and credentials symlinks).
- **YOLO Mode**: Commands are executed automatically without requiring manual approval for tool calls.
- **Live Feedback**: Real-time progress updates with tool usage and preview of the response.
- **Session Control**: Commands to reset sessions, stop running processes, or close topic-specific environments.
- **Dynamic Keyboard**: Custom Telegram keyboards for quick access to frequent commands.

## Commands

- `/start` or `/help`: Displays the main menu and current project context.
- `/list` or `📂 Liste`: Shows a list of configured projects and allows switching or adding new ones.
- `/reload`: Restarts the bot process to apply code changes (requires manual restart via systemd or external script).
- `/reset` or `♻️ Reset`: Clears the current session history for the active project in the current topic.
- `/stop` or `🛑 Stop`: Terminates the currently running Gemini CLI process in the topic.
- `/close` or `🗑 Close`: Completely removes the topic's isolated session folder and deletes the Telegram topic (if permissions allow).
- `➕ Hilfe`: Returns to the main menu.

## Configuration

The bot expects the following environment variables:

- `TELEGRAM_TOKEN`: Your Telegram Bot API token.
- `TELEGRAM_USER`: Your numerical Telegram User ID (only this user can interact with the bot).
- `BOT_START_DIR`: (Optional) The default directory for the main session (defaults to the bot's home).

Projects are stored in `projects.json`, which tracks added directories and the active project for each topic.

## Installation & Setup

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
   Create a `.env` file or export them in your shell:
   ```bash
   export TELEGRAM_TOKEN="your_token"
   export TELEGRAM_USER="your_id"
   ```

4. **Start the bot**:
   ```bash
   ./start_bot.sh [optional_start_dir]
   ```

## Workflow

1. Send any text to the bot to start a new task using the Gemini CLI.
2. The bot will automatically try to resume the "latest" session or start a new one if none exists.
3. You can switch projects using the `/list` command or by clicking `📂 Liste`.
4. To add a new project, click "➕ Neues Projekt hinzufügen" in the list menu and provide the path.

## Requirements

- Python 3.10+
- `python-telegram-bot`
- Gemini CLI installed and configured on the host system.

## License

MIT
