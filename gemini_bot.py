import asyncio, logging, os, sys, shutil, json, signal, re, html
from telegram import Update, ForceReply, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from telegram.error import TelegramError

# --- KONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))
BOT_HOME = os.path.dirname(os.path.abspath(__file__))
PROJECTS_JSON = os.path.join(BOT_HOME, "projects.json")
RELOAD_FILE = os.path.join(BOT_HOME, ".reload_info")
SESSIONS_BASE_DIR = os.path.join(BOT_HOME, "sessions")
GLOBAL_GEMINI_HOME = os.path.expanduser("~") 
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/ai-projects")

os.makedirs(SESSIONS_BASE_DIR, exist_ok=True)
os.makedirs(DEFAULT_PROJECTS_DIR, exist_ok=True)

if not BOT_TOKEN or not ALLOWED_USER_ID:
    print("❌ Fehler: Umgebungsvariablen nicht gefunden!")
    exit(1)

active_processes = {}
active_status_messages = {}
stop_flags = {}
pending_add = {}

def load_config():
    if os.path.exists(PROJECTS_JSON):
        try:
            with open(PROJECTS_JSON, "r") as f:
                data = json.load(f)
                return {"projects": data.get("projects", []), "chat_id": data.get("chat_id")}
        except: pass
    return {"projects": [], "chat_id": None}

config = load_config()
def save_config():
    with open(PROJECTS_JSON, "w") as f: json.dump(config, f, indent=2)

def escape_html(text): return html.escape(str(text)) if text else ""

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📂 Liste"), KeyboardButton("🆕 Neu")],
        [KeyboardButton("🛑 Stop"), KeyboardButton("♻️ Reset")],
        [KeyboardButton("🗑 Close"), KeyboardButton("➕ Hilfe")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_active_project(thread_id):
    for p in config["projects"]:
        if str(p.get("thread_id")) == str(thread_id): return p
    return None

async def sync_topics(application):
    chat_id = config.get("chat_id")
    if not chat_id: return
    changed = False
    for p in config["projects"]:
        if not p.get("thread_id"):
            try:
                topic = await application.bot.create_forum_topic(chat_id=chat_id, name=p["name"])
                p["thread_id"] = topic.message_thread_id
                changed = True
                await application.bot.send_message(chat_id=chat_id, message_thread_id=p["thread_id"],
                                                 text=f"🚀 Projekt <b>{escape_html(p['name'])}</b> bereit.", parse_mode=ParseMode.HTML)
            except Exception as e: 
                logging.error(f"Sync error: {e}")
    if changed: save_config()

async def post_init(application):
    logging.info("🚀 Bot-Initialisierung...")
    commands = [
        BotCommand("start", "Menü"),
        BotCommand("new", "Neu"),
        BotCommand("list", "Liste"),
        BotCommand("reload", "Reload"),
        BotCommand("reset", "Reset"),
        BotCommand("close", "Close"),
        BotCommand("stop", "Stop"),
        BotCommand("help", "Hilfe")
    ]
    await application.bot.set_my_commands(commands)
    await sync_topics(application)
    
    if os.path.exists(RELOAD_FILE):
        try:
            with open(RELOAD_FILE, "r") as f: info = json.load(f)
            os.remove(RELOAD_FILE)
            await application.bot.send_message(chat_id=info["chat_id"], text="✅ <b>Online!</b>", 
                                             parse_mode=ParseMode.HTML, message_thread_id=info.get("thread_id"))
        except: pass

async def run_gemini_command(cmd, path, update, context, status_msg, thread_id):
    topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{thread_id or 'main'}")
    dot_gemini = os.path.join(topic_home, ".gemini")
    os.makedirs(dot_gemini, exist_ok=True)
    
    # System-Instruktion für Dateitransfers hinterlegen
    gemini_md_path = os.path.join(topic_home, "GEMINI.md")
    if not os.path.exists(gemini_md_path):
        with open(gemini_md_path, "w") as f:
            f.write("# Capabilities\nIf the user wants you to provide or show a file you've created or found, you MUST use the following syntax in your response: `UPLOAD_FILE: <path_to_file>`. I will then automatically transmit that file to the user's Telegram chat. Only provide the path, no commentary in that specific line.")

    for f in ["oauth_creds.json", "settings.json", "google_accounts.json"]:
        src, dst = os.path.join(GLOBAL_GEMINI_HOME, ".gemini", f), os.path.join(dot_gemini, f)
        if os.path.exists(src) and not os.path.exists(dst):
            try: os.symlink(src, dst)
            except: pass

    start_time = asyncio.get_event_loop().time()
    full_resp, last_upd, current_tool, stderr_buffer = [], 0, None, []
    env = os.environ.copy()
    env["GEMINI_CLI_HOME"] = topic_home

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, 
                                                   cwd=path, env=env, preexec_fn=os.setsid)
    active_processes[thread_id] = process
    stop_flags[thread_id] = False

    async def update_status(force=False, final_text=None):
        nonlocal last_upd
        if stop_flags.get(thread_id) and not final_text: return
        
        now = asyncio.get_event_loop().time()
        if not force and not final_text and (now - last_upd < 4): return 
        last_upd = now
        
        if final_text:
            txt = final_text
        else:
            elapsed = int(now - start_time)
            txt = f"⏳ <b>Gemini ({escape_html(thread_id or 'main')})</b> ({elapsed}s)\n\n"
            if current_tool: txt += f"🛠 <b>Tool:</b> <code>{escape_html(current_tool)}</code>\n\n"
            if full_resp:
                preview = "".join(full_resp).strip().split('\n')[-3:]
                txt += f"<pre>{escape_html('\\n'.join(preview))}</pre>"
            if stderr_buffer:
                err_lines = [l for l in stderr_buffer if l.strip()][-2:]
                if err_lines: txt += f"\n\n⚠ <b>Log:</b>\n<code>{escape_html('\\n'.join(err_lines))}</code>"

        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
                text=txt, parse_mode=ParseMode.HTML
            )
        except: pass

    async def timer_loop():
        while process.returncode is None:
            await asyncio.sleep(4)
            await update_status()

    async def read_stdout():
        nonlocal current_tool
        while True:
            line = await process.stdout.readline()
            if not line: break
            try:
                decoded_line = line.decode(errors='replace').strip()
                data = json.loads(decoded_line)
                
                if data.get("type") == "message" and data.get("role") == "assistant":
                    content = data.get("content", "")
                    
                    # Check for file upload trigger inside the content
                    match = re.search(r"UPLOAD_FILE:\s*([^\s\"]+)", content)
                    if match:
                        file_path = match.group(1).strip().strip("\"'")
                        if not os.path.isabs(file_path):
                            file_path = os.path.join(path, file_path)
                        
                        if os.path.exists(file_path) and os.path.isfile(file_path):
                            logging.info(f"Uploading file: {file_path}")
                            asyncio.create_task(context.bot.send_document(
                                chat_id=update.effective_chat.id, 
                                document=open(file_path, 'rb'),
                                caption=f"📄 {os.path.basename(file_path)}",
                                message_thread_id=thread_id
                            ))
                        else:
                            logging.error(f"File not found for upload: {file_path}")

                    # Filter out the trigger line from the message sent to the user
                    clean_content = re.sub(r"UPLOAD_FILE:\s*.+", "", content).strip()
                    if clean_content:
                        full_resp.append(clean_content)
                elif data.get("type") == "tool_use":
                    current_tool = data.get("tool_name")
                await update_status()
            except Exception as e:
                logging.debug(f"JSON parse error in stdout: {e}")

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line: break
            text = line.decode(errors='replace').strip()
            if text and not any(x in text for x in ["cached credentials", "YOLO mode"]):
                stderr_buffer.append(text)
                await update_status()

    timer_task = asyncio.create_task(timer_loop())
    try:
        await asyncio.gather(read_stdout(), read_stderr(), process.wait())
    finally:
        timer_task.cancel()
        if thread_id in active_processes: del active_processes[thread_id]
    
    return "".join(full_resp).strip(), process.returncode, "\n".join(stderr_buffer)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ALLOWED_USER_ID: return
    user_text, tid = update.message.text, update.message.message_thread_id
    if not user_text: return
    
    if config["chat_id"] != update.effective_chat.id:
        config["chat_id"] = update.effective_chat.id
        save_config()
        await sync_topics(context.application)

    if pending_add.get(update.effective_user.id):
        pending_add[update.effective_user.id] = False
        path = os.path.join(DEFAULT_PROJECTS_DIR, user_text)
        try:
            if not os.path.exists(path): os.makedirs(path)
            topic = await context.bot.create_forum_topic(chat_id=update.effective_chat.id, name=user_text)
            config["projects"].append({"path": path, "name": user_text, "thread_id": topic.message_thread_id})
            save_config()
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=f"✅ Projekt <b>{escape_html(user_text)}</b> bereit.",
                parse_mode=ParseMode.HTML, message_thread_id=topic.message_thread_id
            )
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {escape_html(e)}", message_thread_id=tid)
            return

    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "🆕 Neu": return await new_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "🗑 Close": return await close_cmd(update, context)
    if user_text == "➕ Hilfe": return await help_cmd(update, context)

    proj = get_active_project(tid)
    path = proj["path"] if proj else BOT_HOME
    
    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ Gemini initialisiert...", message_thread_id=tid)
    active_status_messages[tid] = status_msg
    stop_flags[tid] = False

    # Zusatz-Instruktion für Gemini, um die Dateifähigkeit zu betonen
    enhanced_prompt = user_text + "\n(Reminder: You can send files using 'UPLOAD_FILE: <path>')"

    try:
        cmd = ["gemini", "-r", "latest", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", enhanced_prompt]
        resp, code, err = await run_gemini_command(cmd, path, update, context, status_msg, tid)
        
        if code != 0 and not stop_flags.get(tid) and "No previous sessions found" in (resp + err):
            cmd = ["gemini", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", enhanced_prompt]
            resp, code, err = await run_gemini_command(cmd, path, update, context, status_msg, tid)

        if stop_flags.get(tid): return

        if not resp:
            resp = "✅ Aufgabe erledigt." if code == 0 else f"❌ Fehler ({code})\n<pre>{escape_html(err[-500:])}</pre>"
    except Exception as e: 
        if stop_flags.get(tid): return
        resp = f"⚠ System-Fehler: {escape_html(str(e))}"
    finally:
        if not stop_flags.get(tid):
            if tid in active_status_messages: del active_status_messages[tid]

    if not stop_flags.get(tid):
        # Trigger-Zeilen auch aus der finalen Antwort filtern
        resp = re.sub(r"UPLOAD_FILE:\s*.+", "", resp).strip()
        
        parts = [resp[i:i+4000] for i in range(0, len(resp), 4000)]
        for i, part in enumerate(parts):
            if i == 0:
                try:
                    final_text = f"✅ {part}" if not any(part.startswith(x) for x in ["✅", "❌", "⚠"]) else part
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
                        text=final_text, parse_mode=ParseMode.MARKDOWN
                    )
                except: 
                    try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part)
                    except: pass
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=part, message_thread_id=tid)

# --- COMMANDS ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("🤖 <b>Gemini Bot bereit.</b>\nNutze die Forum-Topics zur Projekttrennung.", 
                                         parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        msg = "<b>Befehle:</b> /new, /list, /reset, /stop, /close, /reload"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    pending_add[update.effective_user.id] = True
    await context.bot.send_message(chat_id=update.effective_chat.id, text="➕ Name für das neue Projekt?", 
                                   reply_markup=ForceReply(selective=True), message_thread_id=update.message.message_thread_id)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    msg = "📂 <b>Aktive Projekte:</b>\n"
    for p in config.get("projects", []):
        msg += f"• <b>{escape_html(p['name'])}</b> (Topic {p['thread_id']})\n"
    await update.message.reply_text(msg or "Keine Projekte.", parse_mode=ParseMode.HTML)

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    tid = update.message.message_thread_id
    if not tid: return
    project = next((p for p in config["projects"] if str(p.get("thread_id")) == str(tid)), None)
    if project:
        config["projects"].remove(project)
        save_config()
        topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{tid}")
        if os.path.exists(topic_home): shutil.rmtree(topic_home)
        try: await context.bot.delete_forum_topic(chat_id=update.effective_chat.id, message_thread_id=tid)
        except: pass
    else: await update.message.reply_text("ℹ Topic nicht zugeordnet.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    tid = update.message.message_thread_id
    process = active_processes.get(tid)
    
    if process and process.returncode is None:
        stop_flags[tid] = True
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            await asyncio.sleep(0.5)
            if process.returncode is None:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except: pass
        
        status_msg = active_status_messages.get(tid)
        if status_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
                    text="🛑 <b>Vom Benutzer abgebrochen.</b>", parse_mode=ParseMode.HTML
                )
            except: pass
            if tid in active_status_messages: del active_status_messages[tid]
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="ℹ Keine aktive Aktion.", message_thread_id=tid)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    tid = update.message.message_thread_id
    topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{tid or 'main'}")
    tmp_dir = os.path.join(topic_home, ".gemini", "tmp")
    if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
    await update.message.reply_text("♻ Session gelöscht.", message_thread_id=tid)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("🔄 Bot wird neu gestartet...", message_thread_id=update.message.message_thread_id)
    with open(RELOAD_FILE, "w") as f:
        json.dump({"chat_id": update.effective_chat.id, "thread_id": update.message.message_thread_id}, f)
    os.execv(sys.executable, [sys.executable] + sys.argv)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
