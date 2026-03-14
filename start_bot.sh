#!/bin/bash

# Das aktuelle Verzeichnis speichern, in dem der User den Befehl aufruft
START_DIR=$(pwd)

# Pfad zu diesem Script-Verzeichnis ermitteln
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "Starte Gemini Telegram Bot..."
echo "Main Session Verzeichnis: $START_DIR"

# Startet den Bot und übergibt das Start-Verzeichnis als Umgebungsvariable
export BOT_START_DIR="$START_DIR"
./venv/bin/python3 gemini_bot.py
