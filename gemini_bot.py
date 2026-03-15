import asyncio
import logging
import subprocess
import os
import json
import signal
import pty
import re
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, BotCommand
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

current_process = None

async def post_init(application):
    commands = [
        BotCommand("start", "Bot starten & Hauptmenü"),
        BotCommand("list", "Projektliste anzeigen"),
        BotCommand("add", "Neues Projekt hinzufügen"),
        BotCommand("reset", "Aktuelle Session löschen"),
        BotCommand("stop", "Laufende Aktion abbrechen"),
        BotCommand("help", "Hilfe & Menü anzeigen")
    ]
    await application.bot.set_my_commands(commands)

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

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO,
                    handlers=[logging.FileHandler(os.path.join(BOT_HOME, "gemini_bot.log")), logging.StreamHandler()])
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📂 Liste"), KeyboardButton("➕ Add")],
        [KeyboardButton("🛑 Stop"), KeyboardButton("♻️ Reset")],
        [KeyboardButton("➕ Hilfe")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="Befehl oder Nachricht...")

# --- NEUE ROBUSTE PTY LOGIK ---
async def run_gemini_command(cmd, path, update, context, status_msg):
    global current_process
    
    logging.info(f"🚀 Starte PTY-Prozess: {' '.join(cmd)}")
    master_fd, slave_fd = pty.openpty()
    start_time = asyncio.get_event_loop().time()
    full_output = []
    
    # Hilfsfunktion zum Lesen vom Master-FD
    def read_from_pty():
        try:
            data = os.read(master_fd, 8192)
            if data:
                text = data.decode(errors='replace')
                text = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)
                full_output.append(text)
        except Exception:
            pass

    # FD zum Event-Loop hinzufügen
    loop = asyncio.get_event_loop()
    loop.add_reader(master_fd, read_from_pty)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=slave_fd, stderr=slave_fd, stdin=slave_fd, cwd=path, preexec_fn=os.setsid
        )
        os.close(slave_fd)
        current_process = process
        
        last_update = asyncio.get_event_loop().time()
        project_name = get_project_name(path)

        while process.returncode is None:
            now = asyncio.get_event_loop().time()
            if now - last_update > 4: # Intervall auf 4s für maximale Stabilität
                last_update = now
                elapsed = int(now - start_time)
                current_text = "".join(full_output)
                last_lines = current_text.strip().split('\n')[-12:]
                
                status_text = f"⏳ *Gemini (@{project_name})* ({elapsed}s)\n\n"
                if last_lines: status_text += "`" + "\n".join(last_lines) + "`"
                else: status_text += "_Warte auf Output..._"
                
                try:
                    # Timeout für Telegram-API Call
                    await asyncio.wait_for(context.bot.edit_message_text(
                        chat_id=update.effective_chat.id, 
                        message_id=status_msg.message_id, 
                        text=status_text, 
                        parse_mode=ParseMode.MARKDOWN
                    ), timeout=3.0)
                except Exception as e:
                    logging.warning(f"⚠️ Telegram Update fehlgeschlagen: {e}")

            await asyncio.sleep(0.5)

        await process.wait()
        
    finally:
        loop.remove_reader(master_fd)
        try: os.close(master_fd)
        except: pass
        current_process = None

    # Finale Antwort zusammenbauen
    response = "".join(full_output).strip()
    elapsed = int(asyncio.get_event_loop().time() - start_time)
    
    # Letztes Status-Update senden
    try:
        final_status = f"✅ *Gemini fertig!* ({elapsed}s)\n\n"
        last_lines = response.split('\n')[-12:]
        if last_lines: final_status += "`" + "\n".join(last_lines) + "`"
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=final_status, parse_mode=ParseMode.MARKDOWN)
    except Exception: pass

    return response, process.returncode

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process
    if update.effective_user.id != ALLOWED_USER_ID: return
    if current_process and current_process.returncode is None:
        try:
            os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
            await update.message.reply_text("🛑 Aktion abgebrochen.", reply_markup=get_main_keyboard())
        except Exception as e:
            await update.message.reply_text(f"⚠️ Fehler: {e}")
    else: await update.message.reply_text("ℹ Keine aktive Aktion.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    user_text = update.message.text
    if not user_text: return
    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "➕ Hilfe": return await start_cmd(update, context)
    if user_text == "➕ Add": return await add_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)

    path = get_active_path()
    status_msg = await update.message.reply_text(f"⏳ Gemini (@{get_project_name(path)})...", parse_mode=ParseMode.MARKDOWN)

    try:
        cmd = ["gemini", "-r", "latest", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
        response_text, retcode = await run_gemini_command(cmd, path, update, context, status_msg)
        if retcode != 0 and "No previous sessions found" in response_text:
            cmd = ["gemini", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
            response_text, retcode = await run_gemini_command(cmd, path, update, context, status_msg)

        if retcode == -signal.SIGTERM or retcode == 15 or retcode == -15: response_text = "🛑 Die Aktion wurde abgebrochen."
        elif not response_text and retcode != 0: response_text = f"❌ CLI Fehler (Code {retcode})"
        elif not response_text: response_text = "Gemini hat die Aufgabe erledigt."
    except Exception as e: response_text = f"⚠ Fehler: {str(e)}"

    parts = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
    for i, part in enumerate(parts):
        try:
            if i == 0: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part, parse_mode=ParseMode.MARKDOWN)
            else: await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                if i == 0: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part)
                else: await update.message.reply_text(part)
            except Exception: pass

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
        await context.bot.send_message(chat_id=update.effective_chat.id, text="➕ Bitte sende mir jetzt den Pfad:", reply_markup=ForceReply(selective=True))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("🤖 *Gemini Power-Bot*\nVerzeichnis: `" + get_active_path() + "`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID or not context.args: return
    path = os.abspath(os.path.join(MAIN_SESSION_DIR, os.path.expanduser(context.args[0])))
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
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", start_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Gemini App-Bot gestartet...")
    application.run_polling()
