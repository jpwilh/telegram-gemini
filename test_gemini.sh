#!/bin/bash
TOPIC_HOME="/home/jpw/ai-projects/bot/sessions/topic_test"
mkdir -p "$TOPIC_HOME/.gemini"
ln -sf /home/jpw/.gemini/oauth_creds.json "$TOPIC_HOME/.gemini/oauth_creds.json"
ln -sf /home/jpw/.gemini/settings.json "$TOPIC_HOME/.gemini/settings.json"

export GEMINI_CLI_HOME="$TOPIC_HOME"
CWD="/home/jpw/ai-projects/bot"

echo "--- TEST 1: Neue Session erstellen ---"
gemini -o text --approval-mode yolo -p "Hallo, mein Name ist TestBot. Merke dir das!"

echo -e "\n--- TEST 2: Session fortsetzen ---"
gemini -r latest -o text --approval-mode yolo -p "Wie war mein Name?"
