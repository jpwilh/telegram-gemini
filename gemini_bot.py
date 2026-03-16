import asyncio
import logging
import subprocess
import os
import sys
import shutil
import json
import signal
import re
import html
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, BotCommand
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, CallbackQueryHandler
from telegram.error import TelegramError, TimedOut, NetworkError

# --- KONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))
BOT_HOME = os.path.dirname(os.path.abspath(__file__))
PROJECTS_JSON = os.path.join(BOT_HOME, "projects.json")
RELOAD_FILE = os.path.join(BOT_HOME, ".reload_info")
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
# Merkt sich, welche Nutzer gerade nach einem Pfad gefragt wurden: {user_id: True}
pending_add = {}

DEFAULT_PROJECTS_DIR = os.path.expanduser("~/ai-projects")
if not os.path.exists(DEFAULT_PROJECTS_DIR):
    os.makedirs(DEFAULT_PROJECTS_DIR)

async def post_init(application):
    commands = [
        BotCommand("start", "Bot starten & Hauptmenü"),
        BotCommand("list", "Projektliste anzeigen"),
        BotCommand("reload", "Bot Code neu laden"),
        BotCommand("reset", "Aktuelle Session löschen"),
        BotCommand("close", "Topic-Session komplett entfernen"),
        BotCommand("stop", "Laufende Aktion abbrechen"),
        BotCommand("help", "Hilfe & Menü anzeigen")
    ]
    await application.bot.set_my_commands(commands)
    
    if os.path.exists(RELOAD_FILE):
        try:
            with open(RELOAD_FILE, "r") as f:
                info = json.load(f)
            os.remove(RELOAD_FILE)
            await application.bot.send_message(
                chat_id=info["chat_id"], 
                text="✅ <b>Bot ist wieder online und einsatzbereit!</b>", 
                parse_mode=ParseMode.HTML,
                message_thread_id=info.get("thread_id")
            )
        except Exception as e:
            logging.error(f"Fehler beim Senden der Reload-Bestätigung: {e}")

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
    topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{thread_id}")
    dot_gemini = os.path.join(topic_home, ".gemini")
    
    if not os.path.exists(dot_gemini):
        os.makedirs(dot_gemini)
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
        [KeyboardButton("♻️ Reset"), KeyboardButton("🗑 Close")],
        [KeyboardButton("➕ Hilfe")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="Nur Text senden...")

def escape_html(text):
    if not text: return ""
    return html.escape(str(text))

async def run_gemini_command(cmd, path, update, context, status_msg, thread_id):
    process_key = (update.effective_chat.id, thread_id)
    topic_home = setup_topic_env(thread_id)
    
    logging.info(f"🚀 Starte Gemini [{thread_id}] in {path}")
    start_time = asyncio.get_event_loop().time()
    
    full_response_parts = []
    last_status_update = 0
    current_tool = None
    stderr_buffer = []

    env = os.environ.copy()
    env["GEMINI_CLI_HOME"] = topic_home

    process = await asyncio.create_subprocess_exec(
        *cmd, 
        stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.PIPE, 
        cwd=path, 
        env=env,
        preexec_fn=os.setsid
    )
    active_processes[process_key] = process

    async def update_status():
        nonlocal last_status_update
        now = asyncio.get_event_loop().time()
        if now - last_status_update < 3: return
        last_status_update = now
        
        elapsed = int(now - start_time)
        status_text = f"⏳ <b>Gemini ({escape_html(thread_id)})</b> ({elapsed}s)\n\n"
        
        if current_tool:
            status_text += f"🛠 <b>Tool:</b> <code>{escape_html(current_tool)}</code>\n\n"
            
        if full_response_parts:
            preview_lines = "".join(full_response_parts).strip().split('\n')[-3:]
            status_text += f"<pre>{escape_html('\\n'.join(preview_lines))}</pre>\n"
            
        if stderr_buffer:
            error_preview = "\n".join([line for line in stderr_buffer if line.strip()][-2:])
            if error_preview:
                status_text += f"⚠ <b>Log/Error:</b>\n<code>{escape_html(error_preview)}</code>"

        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, 
                message_id=status_msg.message_id, 
                text=status_text, 
                parse_mode=ParseMode.HTML
            )
        except: pass

    async def read_stdout():
        nonlocal current_tool
        while True:
            line = await process.stdout.readline()
            if not line: break
            line_text = line.decode(errors='replace').strip()
            if not line_text: continue
            
            try:
                data = json.loads(line_text)
                if data.get("type") == "message":
                    # NUR Nachrichten vom Assistant sammeln (verhindert Spiegeln der Frage)
                    if data.get("role") == "assistant":
                        full_response_parts.append(data.get("content", ""))
                elif data.get("type") == "tool_use":
                    current_tool = data.get("tool_name")
                await update_status()
            except json.JSONDecodeError:
                stderr_buffer.append(f"STDOUT: {line_text}")
                await update_status()

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line: break
            line_text = line.decode(errors='replace').strip()
            if line_text:
                if "Loaded cached credentials" in line_text: continue
                if "YOLO mode is enabled" in line_text: continue
                stderr_buffer.append(line_text)
                await update_status()

    # Führe alles parallel aus
    await asyncio.gather(read_stdout(), read_stderr(), process.wait())
    
    if active_processes.get(process_key) == process: 
        del active_processes[process_key]

    final_response = "".join(full_response_parts).strip()
    elapsed = int(asyncio.get_event_loop().time() - start_time)
    logging.info(f"✅ Gemini [{thread_id}] beendet ({elapsed}s, Code {process.returncode})")
    
    # Status-Meldung am Ende löschen
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    except: pass
    
    return final_response, process.returncode, "\n".join(stderr_buffer)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    user_text = update.message.text
    if not user_text: return
    user_id = update.effective_user.id
    thread_id = update.message.message_thread_id or "default"
    
    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "🗑 Close": return await close_cmd(update, context)
    if user_text == "➕ Hilfe": return await start_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)

    is_reply_to_add = (update.message.reply_to_message and update.message.reply_to_message.text and "Pfad" in update.message.reply_to_message.text)
    if is_reply_to_add or pending_add.get(user_id):
        pending_add[user_id] = False
        if "/" not in user_text and "\\" not in user_text:
            path = os.path.join(DEFAULT_PROJECTS_DIR, user_text)
        else:
            path = os.path.abspath(os.path.expanduser(user_text))
            
        if not os.path.exists(path):
            try:
                os.makedirs(path)
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📁 Verzeichnis neu erstellt: <code>{escape_html(path)}</code>", parse_mode=ParseMode.HTML, message_thread_id=update.message.message_thread_id)
            except Exception as e:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Fehler beim Erstellen von {escape_html(path)}: {escape_html(e)}", message_thread_id=update.message.message_thread_id)
                return

        if os.path.isdir(path):
            if path not in config["projects"]:
                config["projects"].append(path)
                save_config()
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Projekt hinzugefügt:\n<code>{escape_html(path)}</code>", parse_mode=ParseMode.HTML, message_thread_id=update.message.message_thread_id)
            else: 
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"ℹ Bereits vorhanden: <code>{escape_html(path)}</code>", parse_mode=ParseMode.HTML, message_thread_id=update.message.message_thread_id)
        return

    path = get_active_path(thread_id)
    # Statusmeldung als direkte Nachricht (KEIN reply_text -> KEIN Zitat)
    status_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"⏳ Gemini ({escape_html(thread_id)}) @ {escape_html(get_project_name(path))}...", 
        parse_mode=ParseMode.HTML, 
        message_thread_id=update.message.message_thread_id
    )

    try:
        cmd = ["gemini", "-r", "latest", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", user_text]
        response_text, retcode, stderr = await run_gemini_command(cmd, path, update, context, status_msg, thread_id)
        
        if retcode != 0 and "No previous sessions found" in (response_text + stderr):
            cmd = ["gemini", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", user_text]
            response_text, retcode, stderr = await run_gemini_command(cmd, path, update, context, status_msg, thread_id)

        if retcode in [-signal.SIGTERM, 15, -15]: 
            response_text = "🛑 Die Aktion wurde abgebrochen."
        elif not response_text:
            if retcode != 0:
                response_text = f"❌ CLI Fehler (Code {retcode})\n\n<pre>{escape_html(stderr[-500:])}</pre>"
            else:
                response_text = "Gemini hat die Aufgabe erledigt (keine Textantwort)."
    except Exception as e: 
        response_text = f"⚠ System-Fehler: {escape_html(str(e))}"

    parts = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
    for i, part in enumerate(parts):
        try:
            # Finale Antwort als direkte Nachricht (KEIN reply_text -> KEIN Zitat)
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=part, 
                parse_mode=ParseMode.MARKDOWN, 
                message_thread_id=update.message.message_thread_id
            )
        except:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text=part, 
                    message_thread_id=update.message.message_thread_id
                )
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
    await update.message.reply_text("📂 <b>Projekt wählen:</b>", parse_mode=ParseMode.HTML, 
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
        await query.edit_message_text(f"✅ Gewechselt zu: <b>{escape_html(get_project_name(get_active_path(thread_id)))}</b> (Topic {escape_html(thread_id)})", parse_mode=ParseMode.HTML)
    elif query.data == "ask_add":
        pending_add[query.from_user.id] = True
        await context.bot.send_message(chat_id=update.effective_chat.id, 
                                       text="➕ Bitte sende mir jetzt den Pfad (absolut) oder einfach einen Namen für ein neues Verzeichnis in <code>~/ai-projects/</code>:", 
                                       reply_markup=ForceReply(selective=True), parse_mode=ParseMode.HTML, message_thread_id=query.message.message_thread_id)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        thread_id = update.message.message_thread_id or "default"
        await update.message.reply_text(f"🤖 <b>Gemini Power-Bot</b>\nVerzeichnis: <code>{escape_html(get_active_path(thread_id))}</code>", 
                                         parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(), message_thread_id=update.message.message_thread_id)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    thread_id = update.message.message_thread_id or "default"
    process_key = (update.effective_chat.id, thread_id)
    process = active_processes.get(process_key)
    
    if process and process.returncode is None:
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGINT)
            for _ in range(4):
                await asyncio.sleep(0.5)
                if process.returncode is not None: break
            if process.returncode is None:
                os.killpg(pgid, signal.SIGTERM)
            await context.bot.send_message(chat_id=update.effective_chat.id, text="🛑 Aktion wurde abgebrochen.", message_thread_id=update.message.message_thread_id)
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Fehler beim Stoppen: {escape_html(e)}", message_thread_id=update.message.message_thread_id)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="ℹ Keine aktive Aktion.", message_thread_id=update.message.message_thread_id)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    thread_id = update.message.message_thread_id or "default"
    topic_home = setup_topic_env(thread_id)
    await asyncio.create_subprocess_exec("rm", "-rf", os.path.join(topic_home, ".gemini", "tmp"))
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"♻ Session in <b>{escape_html(thread_id)}</b> gelöscht.", parse_mode=ParseMode.HTML, message_thread_id=update.message.message_thread_id)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("🔄 Bot wird neu gestartet... Bitte warten.", message_thread_id=update.message.message_thread_id)
    with open(RELOAD_FILE, "w") as f:
        json.dump({"chat_id": update.effective_chat.id, "thread_id": update.message.message_thread_id}, f)
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    thread_id = update.message.message_thread_id or "default"
    if thread_id == "default":
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Die Main-Session kann nicht geschlossen werden.", message_thread_id=update.message.message_thread_id)
        return
    topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{thread_id}")
    if os.path.exists(topic_home): shutil.rmtree(topic_home)
    if str(thread_id) in config["topics"]:
        del config["topics"][str(thread_id)]
        save_config()
    try:
        await context.bot.delete_forum_topic(chat_id=update.effective_chat.id, message_thread_id=thread_id)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🗑 Topic-Session <b>{escape_html(thread_id)}</b> wurde gelöscht.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🗑 Topic-Session <b>{escape_html(thread_id)}</b> wurde lokal entfernt.", parse_mode=ParseMode.HTML, message_thread_id=update.message.message_thread_id)

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", start_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("reload", reload_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("close", close_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    logging.info("Gemini App-Bot gestartet...")
    application.run_polling()
