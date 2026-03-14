#!/bin/bash

# Pfad zu diesem Script-Verzeichnis ermitteln
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Projektverzeichnis aus Argument lesen, sonst aktuelles Verzeichnis nutzen
PROJECT_DIR="${1:-$(pwd)}"

# Absoluten Pfad auflösen
PROJECT_DIR=$(readlink -f "$PROJECT_DIR")

if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ Fehler: Verzeichnis '$PROJECT_DIR' existiert nicht!"
    exit 1
fi

export GEMINI_PROJECT_DIR="$PROJECT_DIR"
echo "Starte Gemini Telegram Bot für Verzeichnis: $PROJECT_DIR"
# Startet den Bot mit dem Python aus der virtuellen Umgebung
./venv/bin/python3 gemini_bot.py
