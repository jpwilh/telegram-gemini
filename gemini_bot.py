import asyncio
import logging
import subprocess
import os
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler

# --- KONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))

# Wir initialisieren das Verzeichnis (kann später per /cd geändert werden)
CURRENT_PROJECT_DIR = os.environ.get("GEMINI_PROJECT_DIR", os.getcwd())

if not BOT_TOKEN or not ALLOWED_USER_ID:
# ... (Rest der Prüfung bleibt gleich)
# ... (Rest der Prüfung)
    print("❌ Fehler: TELEGRAM_TOKEN oder TELEGRAM_USER Umgebungsvariablen nicht gefunden!")
    print("Bitte stelle sicher, dass du 'source ~/.bashrc' ausgeführt hast.")
    exit(1)
# ---------------------

# Logging konfigurieren: Terminal + Datei
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("gemini_bot.log"),
        logging.StreamHandler()
    ]
)
# Bibliotheken auf WARNING setzen, um 10s-Polling-Logs zu unterdrücken
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

async def notify_admin_of_unauthorized_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Benachrichtigt den Admin über unbefugte Zugriffsversuche."""
    user = update.effective_user
    msg_text = f"🚨 *Warnung: Unbefugter Zugriff!*\n" \
               f"Name: {user.first_name} {user.last_name or ''}\n" \
               f"Username: @{user.username or 'unbekannt'}\n" \
               f"ID: `{user.id}`"
    
    if update.message and update.message.text:
        msg_text += f"\nNachricht: _{update.message.text}_"
    
    logging.warning(f"Unbefugter Zugriff von User ID: {user.id}")
    
    try:
        await context.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=msg_text,
            parse_mode='Markdown'
        )
    except Exception as e:
        logging.error(f"Fehler beim Senden der Admin-Benachrichtigung: {e}")

def split_text(text, limit=4000):
    """Teilt langen Text in kleinere Stücke für Telegram."""
    return [text[i:i+limit] for i in range(0, len(text), limit)]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet normale Textnachrichten."""
    if update.effective_user.id != ALLOWED_USER_ID:
        await notify_admin_of_unauthorized_access(update, context)
        return

    user_text = update.message.text
    if not user_text:
        return

    logging.info(f"📥 Kommando: {user_text[:50]}{'...' if len(user_text) > 50 else ''}")
    status_msg = await update.message.reply_text("⏳ Gemini arbeitet...")

    try:
        # Gemini CLI als Power-Tool aufrufen (YOLO Modus)
        cmd = ["gemini", "-r", "latest", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CURRENT_PROJECT_DIR
        )
        
        stdout, stderr = await process.communicate()
        response_text = stdout.decode().strip()
        error_text = stderr.decode().strip()

        if not response_text and error_text:
            logging.error(f"CLI Fehler: {error_text}")
            response_text = f"❌ CLI Fehler:\n{error_text}"
        elif not response_text:
            response_text = "Gemini hat die Aufgabe erledigt (kein Text-Output)."

    except Exception as e:
        logging.exception("Systemfehler")
        response_text = f"⚠ Ein Fehler ist aufgetreten: {str(e)}"

    # Antwort senden
    try:
        parts = split_text(response_text)
        # Erste Nachricht editieren
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=parts[0]
        )
        # Weitere Teile (falls vorhanden) als neue Nachrichten senden
        for part in parts[1:]:
            await update.message.reply_text(part)
        
        logging.info("📤 Antwort gesendet.")
    except Exception as e:
        logging.error(f"Fehler beim Senden: {e}")
        await update.message.reply_text("Fehler beim Senden der Antwort.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("✅ Bot aktiv. Schick mir Befehle für dein Projekt!")
    else:
        await update.message.reply_text("Zugriff verweigert.")
        await notify_admin_of_unauthorized_access(update, context)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt Infos zum aktuellen Projekt-Verzeichnis."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(f"📍 *Aktuelles Projekt:* `{CURRENT_PROJECT_DIR}`", parse_mode=ParseMode.MARKDOWN)

async def cd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wechselt das Projektverzeichnis."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    
    global CURRENT_PROJECT_DIR
    if not context.args:
        await update.message.reply_text("Benutzung: `/cd <pfad>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    new_path = " ".join(context.args)
    # Tilde (~) auflösen
    new_path = os.path.expanduser(new_path)
    # Absoluten Pfad berechnen
    new_path = os.path.abspath(new_path)
    
    if os.path.isdir(new_path):
        CURRENT_PROJECT_DIR = new_path
        await update.message.reply_text(f"✅ Verzeichnis gewechselt!\n📍 *Neu:* `{CURRENT_PROJECT_DIR}`", parse_mode=ParseMode.MARKDOWN)
        logging.info(f"📂 Projektverzeichnis gewechselt zu: {CURRENT_PROJECT_DIR}")
    else:
        await update.message.reply_text(f"❌ Fehler: Verzeichnis `{new_path}` existiert nicht!", parse_mode=ParseMode.MARKDOWN)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await notify_admin_of_unauthorized_access(update, context)
        return

    try:
        # Reset via CLI im aktuellen Projektverzeichnis
        process = await asyncio.create_subprocess_exec(
            "gemini", "--delete-session", "latest",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CURRENT_PROJECT_DIR
        )
        await process.communicate()
        await update.message.reply_text("✅ Session gelöscht. Neuer Kontext gestartet.")
        logging.info(f"♻ Session Reset durchgeführt für {CURRENT_PROJECT_DIR}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Reset fehlgeschlagen: {e}")

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("info", info_cmd))
    application.add_handler(CommandHandler("cd", cd_cmd))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print(f"Gemini Telegram Bot ist gestartet (Projekt: {CURRENT_PROJECT_DIR})...")
    application.run_polling()
