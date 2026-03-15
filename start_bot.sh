#!/bin/bash

# Das Verzeichnis ermitteln, in dem das Script selbst liegt
BOT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$BOT_DIR"

# Das neue Standard-Verzeichnis (Playground) festlegen
PLAYGROUND_DIR="/home/jpw/ai-projects/playground"

# Falls der User ein Argument übergibt, wird das als Start-Verzeichnis genutzt
# Ansonsten wird 'playground' als Main Session (Index 0) genutzt
START_DIR="${1:-$PLAYGROUND_DIR}"

if [ ! -d "$START_DIR" ]; then
    echo "⚠️ Verzeichnis '$START_DIR' nicht gefunden. Erstelle es..."
    mkdir -p "$START_DIR"
fi

echo "Starte Gemini Telegram Bot..."
echo "Main Session Verzeichnis: $START_DIR"

# Startet den Bot mit dem Python aus der virtuellen Umgebung
export BOT_START_DIR="$START_DIR"
./venv/bin/python3 gemini_bot.py
