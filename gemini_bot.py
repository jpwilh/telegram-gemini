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
from telegram.error import TelegramError, TimedOut, NetworkError

# --- KONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))
BOT_HOME = os.path.dirname(os.path.abspath(__file__))
PROJECTS_JSON = os.path.join(BOT_HOME, "projects.json")
MAIN_SESSION_DIR = os.environ.get("BOT_START_DIR", BOT_HOME)
SESSIONS_BASE_DIR = os.path.join(BOT_HOME, "sessions")
GLOBAL_GEMINI_HOME = os.path.expanduser("~") # Wo die globalen .gemini/ Daten liegen

if not os.path.exists(SESSIONS_BASE_DIR):
    os.makedirs(SESSIONS_BASE_DIR)

if not BOT_TOKEN or not ALLOWED_USER_ID:
    print("❌ Fehler: Umgebungsvariablen nicht gefunden!")
    exit(1)

# Dictionary für aktive Prozesse pro Topic: {(chat_id, thread_id): process}
active_processes = {}

async def post_init(application):
    commands = [
        BotCommand("start", "Bot starten & Hauptmenü"),
        BotCommand("list", "Projektliste anzeigen"),
        BotCommand("reset", "Aktuelle Session löschen"),
        BotCommand("stop", "Laufende Aktion abbrechen"),
        BotCommand("help", "Hilfe & Menü anzeigen")
    ]
    await application.bot.set_my_commands(commands)

def load_config():
    if os.path.exists(PROJECTS_JSON):
        try:
            with open(PROJECTS_JSON, "r") as f:
                data = json.load(f)
                if "topics" not in data: data["topics"] = {}
                return data
        except Exception: pass
    return {"topics": {}, "projects": []}

config = load_config()

def save_config():
    with open(PROJECTS_JSON, "w") as f:
        json.dump(config, f, indent=2)

def setup_topic_env(thread_id):
    """Bereitet das isolierte GEMINI_CLI_HOME für das Topic vor."""
    topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{thread_id}")
    dot_gemini = os.path.join(topic_home, ".gemini")
    
    if not os.path.exists(dot_gemini):
        os.makedirs(dot_gemini)
        # Symlinks zu globalen Auth-Daten erstellen
        for f in ["oauth_creds.json", "settings.json", "google_accounts.json"]:
            src = os.path.join(GLOBAL_GEMINI_HOME, ".gemini", f)
            dst = os.path.join(dot_gemini, f)
            if os.path.exists(src) and not os.path.exists(dst):
                try: os.symlink(src, dst)
                except: pass
    return topic_home

def get_active_path(thread_id="default"):
    t_id = str(thread_id)
    idx = config["topics"].get(t_id, 0)
    if idx == 0 or not config.get("projects"): return MAIN_SESSION_DIR
    p_idx = idx - 1
    return config["projects"][p_idx] if 0 <= p_idx < len(config["projects"]) else MAIN_SESSION_DIR

def get_project_name(path):
    return os.path.basename(path) or path

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO,
                    handlers=[logging.FileHandler(os.path.join(BOT_HOME, "gemini_bot.log")), logging.StreamHandler()])
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📂 Liste"), KeyboardButton("🛑 Stop")],
        [KeyboardButton("♻️ Reset"), KeyboardButton("➕ Hilfe")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="Befehl oder Nachricht...")

def escape_markdown(text):
    if not text: return ""
    return text.replace("`", "'")

async def run_gemini_command(cmd, path, update, context, status_msg, thread_id):
    process_key = (update.effective_chat.id, thread_id)
    topic_home = setup_topic_env(thread_id)
    
    logging.info(f"🚀 Starte Gemini [{thread_id}] in {path}")
    master_fd, slave_fd = pty.openpty()
    start_time = asyncio.get_event_loop().time()
    full_output = []
    
    def read_from_pty():
        try:
            data = os.read(master_fd, 8192)
            if data:
                text = data.decode(errors='replace')
                text = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)
                full_output.append(text)
        except Exception: pass

    loop = asyncio.get_event_loop()
    loop.add_reader(master_fd, read_from_pty)

    env = os.environ.copy()
    env["GEMINI_CLI_HOME"] = topic_home

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=slave_fd, stderr=slave_fd, stdin=slave_fd, cwd=path, 
            preexec_fn=os.setsid, env=env
        )
        os.close(slave_fd)
        active_processes[process_key] = process
        
        last_update = asyncio.get_event_loop().time()

        while process.returncode is None:
            now = asyncio.get_event_loop().time()
            if now - last_update > 4:
                last_update = now
                elapsed = int(now - start_time)
                current_text = "".join(full_output)
                last_lines = current_text.strip().split('\n')[-10:]
                
                clean_lines = escape_markdown("\n".join(last_lines))
                status_text = f"⏳ *Gemini ({thread_id})* ({elapsed}s)\n\n"
                if clean_lines: status_text += f"```\n{clean_lines}\n```"
                else: status_text += "_Warte auf Output..._"
                
                try:
                    await asyncio.wait_for(context.bot.edit_message_text(
                        chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
                        text=status_text, parse_mode=ParseMode.MARKDOWN
                    ), timeout=3.0)
                except:
                    try:
                        plain_text = status_text.replace("*", "").replace("_", "").replace("```", "")
                        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=plain_text)
                    except: pass
            await asyncio.sleep(0.5)
        await process.wait()
    finally:
        loop.remove_reader(master_fd)
        try: os.close(master_fd)
        except: pass
        if active_processes.get(process_key) == process: del active_processes[process_key]

    response = "".join(full_output).strip()
    elapsed = int(asyncio.get_event_loop().time() - start_time)
    
    try:
        clean_response = escape_markdown("\n".join(response.split('\n')[-10:]))
        final_status = f"✅ *Gemini fertig!* ({elapsed}s)\n\n"
        if clean_response: final_status += f"```\n{clean_response}\n```"
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=final_status, parse_mode=ParseMode.MARKDOWN)
    except:
        try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=f"✅ Gemini fertig! ({elapsed}s)")
        except: pass
    return response, process.returncode

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    user_text = update.message.text
    if not user_text: return
    thread_id = update.message.message_thread_id or "default"
    
    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "➕ Hilfe": return await start_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)

    # Wenn Nachricht eine Antwort auf "Bitte sende mir jetzt den Pfad" ist:
    if update.message.reply_to_message and "Pfad" in update.message.reply_to_message.text:
        path = os.abspath(os.path.expanduser(user_text))
        if os.path.isdir(path):
            if path not in config["projects"]:
                config["projects"].append(path)
                save_config()
                await update.message.reply_text(f"✅ Projekt hinzugefügt:\n`{path}`", parse_mode=ParseMode.MARKDOWN, message_thread_id=update.message.message_thread_id)
            else: await update.message.reply_text("ℹ Bereits vorhanden.", message_thread_id=update.message.message_thread_id)
        else: await update.message.reply_text(f"❌ Verzeichnis nicht gefunden: `{path}`", message_thread_id=update.message.message_thread_id)
        return

    path = get_active_path(thread_id)
    status_msg = await update.message.reply_text(f"⏳ Gemini ({thread_id}) @ {get_project_name(path)}...", 
                                                 parse_mode=ParseMode.MARKDOWN, message_thread_id=update.message.message_thread_id)

    try:
        # Resume mit 'latest' innerhalb des isolierten topic_home
        cmd = ["gemini", "-r", "latest", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
        response_text, retcode = await run_gemini_command(cmd, path, update, context, status_msg, thread_id)
        
        # Fallback falls keine Session existiert
        if retcode != 0 and "No previous sessions found" in response_text:
            cmd = ["gemini", "-o", "text", "--approval-mode", "yolo", "-p", user_text]
            response_text, retcode = await run_gemini_command(cmd, path, update, context, status_msg, thread_id)

        if retcode in [-signal.SIGTERM, 15, -15]: response_text = "🛑 Die Aktion wurde abgebrochen."
        elif not response_text and retcode != 0: response_text = f"❌ CLI Fehler (Code {retcode})"
        elif not response_text: response_text = "Gemini hat die Aufgabe erledigt."
    except Exception as e: response_text = f"⚠ Fehler: {str(e)}"

    parts = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
    for i, part in enumerate(parts):
        try:
            if i == 0: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part, parse_mode=ParseMode.MARKDOWN)
            else: await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN, message_thread_id=update.message.message_thread_id)
        except:
            try:
                if i == 0: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part)
                else: await update.message.reply_text(part, message_thread_id=update.message.message_thread_id)
            except: pass

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    thread_id = update.message.message_thread_id or "default"
    current_idx = config["topics"].get(str(thread_id), 0)
    buttons = [[InlineKeyboardButton("🏠 0: Main Session (./)" + (" ✅" if current_idx == 0 else ""), callback_data="proj_0")]]
    for i, p in enumerate(config.get("projects", []), 1):
        label = (f"✅ {i}: {get_project_name(p)}" if current_idx == i else f"🚀 {i}: {get_project_name(p)}")
        buttons.append([InlineKeyboardButton(label, callback_data=f"proj_{i}")])
    buttons.append([InlineKeyboardButton("➕ Neues Projekt hinzufügen", callback_data="ask_add")])
    await update.message.reply_text("📂 *Projekt wählen:*", parse_mode=ParseMode.MARKDOWN, 
                                     reply_markup=InlineKeyboardMarkup(buttons), message_thread_id=update.message.message_thread_id)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID: return
    try: await query.answer()
    except: pass
    thread_id = query.message.message_thread_id or "default"
    if query.data.startswith("proj_"):
        idx = int(query.data.split("_")[1])
        config["topics"][str(thread_id)] = idx
        save_config()
        await query.edit_message_text(f"✅ Gewechselt zu: *{get_project_name(get_active_path(thread_id))}* (Topic {thread_id})", parse_mode=ParseMode.MARKDOWN)
    elif query.data == "ask_add":
        await context.bot.send_message(chat_id=update.effective_chat.id, text="➕ Bitte sende mir jetzt den Pfad:", 
                                       reply_markup=ForceReply(selective=True), message_thread_id=query.message.message_thread_id)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        thread_id = update.message.message_thread_id or "default"
        await update.message.reply_text(f"🤖 *Gemini Power-Bot*\nVerzeichnis: `{get_active_path(thread_id)}`", 
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(), message_thread_id=update.message.message_thread_id)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    thread_id = update.message.message_thread_id or "default"
    process_key = (update.effective_chat.id, thread_id)
    process = active_processes.get(process_key)
    if process and process.returncode is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            await update.message.reply_text("🛑 Aktion abgebrochen.", message_thread_id=update.message.message_thread_id)
        except: pass
    else: await update.message.reply_text("ℹ Keine aktive Aktion.", message_thread_id=update.message.message_thread_id)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    thread_id = update.message.message_thread_id or "default"
    topic_home = setup_topic_env(thread_id)
    await asyncio.create_subprocess_exec("rm", "-rf", os.path.join(topic_home, ".gemini", "tmp"))
    await update.message.reply_text(f"♻ Session in *{thread_id}* gelöscht.", parse_mode=ParseMode.MARKDOWN, message_thread_id=update.message.message_thread_id)

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", start_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Gemini App-Bot gestartet...")
    application.run_polling()
