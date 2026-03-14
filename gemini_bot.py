import asyncio
import logging
import subprocess
import os
import json
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler

# --- KONFIGURATION ---
# Liest die Daten aus deinen Umgebungsvariablen (.bashrc)
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))

# Festes Basisverzeichnis (wo der Bot liegt)
BOT_HOME = os.path.dirname(os.path.abspath(__file__))
MAIN_SESSION_DIR = os.environ.get("BOT_START_DIR", BOT_HOME)
PROJECTS_JSON = os.path.join(BOT_HOME, "projects.json")

if not BOT_TOKEN or not ALLOWED_USER_ID:
    print("❌ Fehler: TELEGRAM_TOKEN oder TELEGRAM_USER Umgebungsvariablen nicht gefunden!")
    exit(1)

# --- PROJEKT MANAGEMENT ---
def load_config():
    if os.path.exists(PROJECTS_JSON):
        try:
            with open(PROJECTS_JSON, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Fehler beim Laden der Projekte: {e}")
    return {"active_index": 0, "projects": []}

config = load_config()

def save_config():
    try:
        with open(PROJECTS_JSON, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Fehler beim Speichern der Projekte: {e}")

def get_active_path():
    if config["active_index"] == 0 or not config.get("projects"):
        return MAIN_SESSION_DIR
    idx = config["active_index"] - 1
    if 0 <= idx < len(config["projects"]):
        return config["projects"][idx]
    return MAIN_SESSION_DIR

def get_project_name(path):
    return os.path.basename(path) or path

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(BOT_HOME, "gemini_bot.log")),
        logging.StreamHandler()
    ]
)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- BOT LOGIK ---
async def notify_admin_of_unauthorized_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_text = f"🚨 *Warnung: Unbefugter Zugriff!*\n" \
               f"Name: {user.first_name} {user.last_name or ''}\n" \
               f"Username: @{user.username or 'unbekannt'}\n" \
               f"ID: `{user.id}`"
    
    logging.warning(f"Unbefugter Zugriff von User ID: {user.id}")
    try:
        await context.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=msg_text,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logging.error(f"Fehler beim Senden der Admin-Benachrichtigung: {e}")

def split_text(text, limit=4000):
    return [text[i:i+limit] for i in range(0, len(text), limit)]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await notify_admin_of_unauthorized_access(update, context)
        return

    user_text = update.message.text
    if not user_text:
        return

    path = get_active_path()
    logging.info(f"📥 [{get_project_name(path)}] Kommando: {user_text[:50]}{'...' if len(user_text) > 50 else ''}")
    status_msg = await update.message.reply_text(f"⏳ Gemini arbeitet in *{get_project_name(path)}*...", parse_mode=ParseMode.MARKDOWN)

    try:
        # Gemini CLI im aktiven Projektpfad ausführen
        cmd = ["gemini", "-r", "latest", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path
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
    parts = split_text(response_text)
    for i, part in enumerate(parts):
        try:
            if i == 0:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=part,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            # Fallback auf Reintext
            if i == 0:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=part
                )
            else:
                await update.message.reply_text(part)
    
    logging.info("📤 Antwort gesendet.")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return

    msg = "📂 *Deine Projekte:*\n\n"
    # Main Projekt
    icon = "✅" if config["active_index"] == 0 else "0️⃣"
    msg += f"{icon} *0: Main Session*\n   `./` (Start-Verzeichnis)\n\n"

    # Weitere Projekte
    for i, p in enumerate(config.get("projects", []), 1):
        icon = "✅" if config["active_index"] == i else f"{i}️⃣"
        # Pfad relativ zum Start-Verzeichnis anzeigen
        rel_path = os.path.relpath(p, MAIN_SESSION_DIR)
        msg += f"{icon} *{i}: {get_project_name(p)}*\n   `{rel_path}`\n\n"

    msg += "Nutze `/proj <nr>` zum Wechseln oder `/add <pfad>`."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def proj_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    try:
        idx = int(context.args[0])
        if 0 <= idx <= len(config.get("projects", [])):
            config["active_index"] = idx
            save_config()
            path = get_active_path()
            await update.message.reply_text(f"🚀 Gewechselt zu: *{get_project_name(path)}*\n`{path}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Ungültige Projektnummer.")
    except Exception:
        await update.message.reply_text("Verwendung: `/proj <nummer>`")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    if not context.args:
        await update.message.reply_text("Verwendung: `/add <pfad>` (absolut oder relativ zum Start-Verzeichnis)")
        return
    
    raw_path = context.args[0]
    # Relativen Pfad IMMER gegen das ursprüngliche Start-Verzeichnis auflösen
    path = os.path.abspath(os.path.join(MAIN_SESSION_DIR, os.path.expanduser(raw_path)))
    
    if os.path.isdir(path):
        if path not in config["projects"]:
            config["projects"].append(path)
            save_config()
            await update.message.reply_text(f"✅ Projekt hinzugefügt:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("ℹ Projekt ist bereits in der Liste.")
    else:
        await update.message.reply_text(f"❌ Verzeichnis nicht gefunden:\n`{path}`")

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    try:
        idx = int(context.args[0])
        if 1 <= idx <= len(config["projects"]):
            removed = config["projects"].pop(idx-1)
            config["active_index"] = 0 # Zurück zu Main
            save_config()
            await update.message.reply_text(f"🗑 Projekt entfernt:\n`{removed}`\n\nAktives Projekt wurde auf *Main* zurückgesetzt.")
        else:
            await update.message.reply_text("❌ Ungültige Nummer (0 kann nicht gelöscht werden).")
    except Exception:
        await update.message.reply_text("Verwendung: `/del <nummer>`")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    path = get_active_path()
    try:
        await asyncio.create_subprocess_exec("gemini", "--delete-session", "latest", cwd=path)
        await update.message.reply_text(f"♻ Session in *{get_project_name(path)}* wurde zurückgesetzt.")
        logging.info(f"♻ Session Reset in {path}")
    except Exception as e:
        await update.message.reply_text(f"❌ Reset fehlgeschlagen: {e}")

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", list_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("proj", proj_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("del", del_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print(f"Gemini Multi-Project Bot gestartet (Modus: YOLO)...")
    application.run_polling()
