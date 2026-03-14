import asyncio
import logging
import subprocess
import os
import json
import signal
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, CallbackQueryHandler

# --- KONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))
BOT_HOME = os.path.dirname(os.path.abspath(__file__))
PROJECTS_JSON = os.path.join(BOT_HOME, "projects.json")
MAIN_SESSION_DIR = os.environ.get("BOT_START_DIR", BOT_HOME)

if not BOT_TOKEN or not ALLOWED_USER_ID:
    print("❌ Fehler: Umgebungsvariablen nicht gefunden!")
    exit(1)

# Globaler Tracker für den laufenden Prozess
current_process = None

# --- PROJEKT MANAGEMENT ---
def load_config():
    if os.path.exists(PROJECTS_JSON):
        try:
            with open(PROJECTS_JSON, "r") as f:
                return json.load(f)
        except Exception: pass
    return {"active_index": 0, "projects": []}

config = load_config()

def save_config():
    with open(PROJECTS_JSON, "w") as f:
        json.dump(config, f, indent=2)

def get_active_path():
    if config["active_index"] == 0 or not config.get("projects"):
        return MAIN_SESSION_DIR
    idx = config["active_index"] - 1
    return config["projects"][idx] if 0 <= idx < len(config["projects"]) else MAIN_SESSION_DIR

def get_project_name(path):
    return os.path.basename(path) or path

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO,
                    handlers=[logging.FileHandler(os.path.join(BOT_HOME, "gemini_bot.log")), logging.StreamHandler()])
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- KEYBOARDS ---
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📂 Liste"), KeyboardButton("➕ Add")],
        [KeyboardButton("🛑 Stop"), KeyboardButton("♻️ Reset")],
        [KeyboardButton("➕ Hilfe")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="Befehl oder Nachricht...")

# --- BOT LOGIK ---
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Beendet den aktuell laufenden Prozess."""
    global current_process
    if update.effective_user.id != ALLOWED_USER_ID: return
    
    if current_process and current_process.returncode is None:
        current_process.terminate()
        await update.message.reply_text("🛑 Aktion abgebrochen.", reply_markup=get_main_keyboard())
        logging.info("🛑 Prozess durch User abgebrochen.")
    else:
        await update.message.reply_text("ℹ Keine aktive Aktion zum Abbrechen.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process
    if update.effective_user.id != ALLOWED_USER_ID: return
    
    user_text = update.message.text
    if not user_text: return

    # Menü-Buttons abfangen
    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "➕ Hilfe": return await start_cmd(update, context)
    if user_text == "➕ Add": return await add_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)

    if update.message.reply_to_message and "Bitte sende mir jetzt den Pfad" in update.message.reply_to_message.text:
        context.args = [user_text]
        return await add_cmd(update, context)

    path = get_active_path()
    logging.info(f"📥 [{get_project_name(path)}] {user_text[:50]}")
    status_msg = await update.message.reply_text(f"⏳ Gemini (@{get_project_name(path)})...", parse_mode=ParseMode.MARKDOWN)

    try:
        # Erster Versuch: Resume
        cmd = ["gemini", "-r", "latest", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
        current_process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=path)
        stdout, stderr = await current_process.communicate()
        
        response_text = stdout.decode().strip()
        error_text = stderr.decode().strip()

        # Fallback für neue Session
        if "No previous sessions found" in error_text:
            cmd = ["gemini", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
            current_process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=path)
            stdout, stderr = await current_process.communicate()
            response_text = stdout.decode().strip()
            error_text = stderr.decode().strip()

        if current_process.returncode == -signal.SIGTERM or current_process.returncode == 15:
            response_text = "🛑 Die Aktion wurde abgebrochen."
        elif not response_text and error_text:
            response_text = f"❌ CLI Fehler:\n{error_text}"
        elif not response_text:
            response_text = "Gemini hat die Aufgabe erledigt."

    except Exception as e:
        response_text = f"⚠ Fehler: {str(e)}"
    finally:
        current_process = None

    parts = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
    for i, part in enumerate(parts):
        try:
            if i == 0: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part, parse_mode=ParseMode.MARKDOWN)
            else: await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            if i == 0: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part)
            else: await update.message.reply_text(part)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    buttons = [[InlineKeyboardButton("🏠 0: Main Session (./)", callback_data="proj_0")]]
    for i, p in enumerate(config.get("projects", []), 1):
        name = get_project_name(p)
        label = f"✅ {i}: {name}" if config["active_index"] == i else f"🚀 {i}: {name}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"proj_{i}")])
    buttons.append([InlineKeyboardButton("➕ Neues Projekt hinzufügen", callback_data="ask_add")])
    await update.message.reply_text("📂 *Projekt wählen:*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID: return
    await query.answer()
    if query.data.startswith("proj_"):
        idx = int(query.data.split("_")[1])
        config["active_index"] = idx
        save_config()
        await query.edit_message_text(f"✅ Gewechselt zu: *{get_project_name(get_active_path())}*", parse_mode=ParseMode.MARKDOWN)
    elif query.data == "ask_add":
        await context.bot.send_message(chat_id=update.effective_chat.id, text="➕ Bitte sende mir jetzt den Pfad für das neue Projekt (absolut oder relativ zum Start-Verzeichnis):", reply_markup=ForceReply(selective=True))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("🤖 *Gemini Power-Bot*\nVerzeichnis: `" + get_active_path() + "`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
    else: await update.message.reply_text("Zugriff verweigert.")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    if not context.args:
        return await update.message.reply_text("➕ Bitte sende mir jetzt den Pfad für das neue Projekt (absolut oder relativ zum Start-Verzeichnis):", reply_markup=ForceReply(selective=True))
    
    path = os.path.abspath(os.path.join(MAIN_SESSION_DIR, os.path.expanduser(context.args[0])))
    if os.path.isdir(path):
        if path not in config["projects"]:
            config["projects"].append(path)
            save_config()
            await update.message.reply_text(f"✅ Projekt hinzugefügt:\n`{path}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
        else: await update.message.reply_text("ℹ Bereits vorhanden.")
    else: await update.message.reply_text(f"❌ Verzeichnis nicht gefunden: `{path}`")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    path = get_active_path()
    await asyncio.create_subprocess_exec("gemini", "--delete-session", "latest", cwd=path)
    await update.message.reply_text(f"♻ Session in *{get_project_name(path)}* gelöscht.", parse_mode=ParseMode.MARKDOWN)

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Gemini App-Bot gestartet...")
    application.run_polling()
