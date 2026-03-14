#!/bin/bash

# Pfad zu diesem Script-Verzeichnis ermitteln
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "Starte Gemini Telegram Bot..."
# Startet den Bot mit dem Python aus der virtuellen Umgebung
./venv/bin/python3 gemini_bot.py
