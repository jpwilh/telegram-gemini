import asyncio, logging, os, sys, shutil, json, signal, re, html
from telegram import Update, ForceReply, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler

# --- KONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER", 0))
BOT_HOME = os.path.dirname(os.path.abspath(__file__))
PROJECTS_JSON = os.path.join(BOT_HOME, "projects.json")
RELOAD_FILE = os.path.join(BOT_HOME, ".reload_info")
SESSIONS_BASE_DIR = os.path.join(BOT_HOME, "sessions")
GLOBAL_GEMINI_HOME = os.path.expanduser("~") 
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/ai-projects")

for d in [SESSIONS_BASE_DIR, DEFAULT_PROJECTS_DIR]: os.makedirs(d, exist_ok=True)

if not BOT_TOKEN or not ALLOWED_USER_ID:
    print("❌ Fehler: Umgebungsvariablen (TOKEN/USER) fehlen!"); exit(1)

# Globaler State
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
    return ReplyKeyboardMarkup([
        [KeyboardButton("📂 Liste"), KeyboardButton("🆕 Neu")],
        [KeyboardButton("🛑 Stop"), KeyboardButton("♻️ Reset")],
        [KeyboardButton("🗑 Close"), KeyboardButton("➕ Hilfe")]
    ], resize_keyboard=True)

async def post_init(application):
    logging.info("🚀 Bot-Initialisierung...")
    # Chat-ID Reload Bestätigung
    if os.path.exists(RELOAD_FILE):
        try:
            with open(RELOAD_FILE, "r") as f: info = json.load(f)
            os.remove(RELOAD_FILE)
            await application.bot.send_message(chat_id=info["chat_id"], text="✅ <b>Online!</b>", 
                                             parse_mode=ParseMode.HTML, message_thread_id=info.get("thread_id"))
        except: pass
    # Topic Sync
    await sync_topics(application)

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
            except Exception as e: logging.error(f"Sync error: {e}")
    if changed: save_config()

async def run_gemini_command(cmd, path, update, context, status_msg, thread_id):
    topic_home = os.path.join(SESSIONS_BASE_DIR, f"topic_{thread_id or 'main'}")
    dot_gemini = os.path.join(topic_home, ".gemini")
    os.makedirs(dot_gemini, exist_ok=True)
    
    # Auth-Links sicherstellen
    for f in ["oauth_creds.json", "settings.json", "google_accounts.json"]:
        src, dst = os.path.join(GLOBAL_GEMINI_HOME, ".gemini", f), os.path.join(dot_gemini, f)
        if os.path.exists(src) and not os.path.exists(dst):
            try: os.symlink(src, dst)
            except: pass

    start_time = asyncio.get_event_loop().time()
    last_activity = start_time
    full_resp, last_upd, current_tool, stderr_buffer = [], 0, None, []
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.PIPE, cwd=path, env={**os.environ, "GEMINI_CLI_HOME": topic_home}, preexec_fn=os.setsid)
    
    active_processes[thread_id] = process
    stop_flags[thread_id] = False
    timed_out = False

    async def update_status(force=False):
        nonlocal last_upd
        if stop_flags.get(thread_id): return
        now = asyncio.get_event_loop().time()
        if not force and (now - last_upd < 4): return 
        last_upd = now
        elapsed = int(now - start_time)
        txt = f"⏳ <b>Gemini ({escape_html(thread_id or 'main')})</b> ({elapsed}s)\n\n"
        if current_tool: txt += f"🛠 <b>Tool:</b> <code>{escape_html(current_tool)}</code>\n\n"
        if full_resp:
            preview = "".join(full_resp).strip().split('\n')[-3:]
            txt += f"<pre>{escape_html('\\n'.join(preview))}</pre>"
        if stderr_buffer:
            err = "\n".join([l for l in stderr_buffer if l.strip()][-2:])
            if err: txt += f"\n\n⚠ <b>Log:</b>\n<code>{escape_html(err)}</code>"
        try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=txt, parse_mode=ParseMode.HTML)
        except: pass

    async def watchdog():
        nonlocal timed_out
        while process.returncode is None:
            await asyncio.sleep(5)
            if asyncio.get_event_loop().time() - last_activity > 300:
                try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except: pass
                timed_out = True; break
            await update_status()

    async def read_stream(stream, is_stderr=False):
        nonlocal current_tool, last_activity
        while True:
            line = await stream.readline()
            if not line: break
            last_activity = asyncio.get_event_loop().time()
            text = line.decode(errors='replace').strip()
            if is_stderr:
                if text and not any(x in text for x in ["cached credentials", "YOLO mode"]):
                    stderr_buffer.append(text)
            else:
                try:
                    data = json.loads(text)
                    if data.get("type") == "message" and data.get("role") == "assistant":
                        full_resp.append(data.get("content", ""))
                    elif data.get("type") == "tool_use": current_tool = data.get("tool_name")
                except: pass
            await update_status()

    wd_task = asyncio.create_task(watchdog())
    await asyncio.gather(read_stream(process.stdout), read_stream(process.stderr, True), process.wait())
    wd_task.cancel()
    
    if thread_id in active_processes: del active_processes[thread_id]
    
    res = "".join(full_resp).strip()
    if timed_out: res = "❌ <b>Abbruch:</b> Inaktivitäts-Timeout (5 Min)."
    return res, process.returncode, "\n".join(stderr_buffer)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ALLOWED_USER_ID: return
    user_text, tid = update.message.text, update.message.message_thread_id
    if not user_text: return
    
    if config["chat_id"] != update.effective_chat.id:
        config["chat_id"] = update.effective_chat.id; save_config()
        await sync_topics(context.application)

    # Projekt-Anlage
    if pending_add.get(update.effective_user.id):
        pending_add[update.effective_user.id] = False
        path = os.path.join(DEFAULT_PROJECTS_DIR, user_text)
        try:
            os.makedirs(path, exist_ok=True)
            topic = await context.bot.create_forum_topic(chat_id=update.effective_chat.id, name=user_text)
            config["projects"].append({"path": path, "name": user_text, "thread_id": topic.message_thread_id})
            save_config()
            await context.bot.send_message(chat_id=update.effective_chat.id, message_thread_id=topic.message_thread_id,
                text=f"✅ Projekt <b>{escape_html(user_text)}</b> bereit.", parse_mode=ParseMode.HTML)
        except Exception as e: await update.message.reply_text(f"❌ Fehler: {e}")
        return

    # Buttons / Kommandos
    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "🆕 Neu": return await new_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "🗑 Close": return await close_cmd(update, context)
    if user_text == "➕ Hilfe": return await help_cmd(update, context)

    # Concurrency Check
    if tid in active_processes:
        return await update.message.reply_text("⏳ <b>Gemini arbeitet noch...</b>", parse_mode=ParseMode.HTML)

    proj = next((p for p in config["projects"] if str(p.get("thread_id")) == str(tid)), None)
    path = proj["path"] if proj else BOT_HOME
    
    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ Gemini denkt nach...", message_thread_id=tid)
    active_status_messages[tid] = status_msg

    try:
        cmd_base = ["gemini", "-r", "latest", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", user_text]
        resp, code, err = await run_gemini_command(cmd_base, path, update, context, status_msg, tid)
        
        if code != 0 and not stop_flags.get(tid) and not resp.startswith("❌") and "No previous sessions found" in (resp + err):
            resp, code, err = await run_gemini_command(cmd_base[2:], path, update, context, status_msg, tid)

        if stop_flags.get(tid): return

        if not resp:
            resp = "✅ Erledigt." if code == 0 else f"❌ Fehler ({code})\n<pre>{escape_html(err[-500:])}</pre>"
    except Exception as e: 
        if stop_flags.get(tid): return
        resp = f"⚠ System-Fehler: {escape_html(str(e))}"
    finally:
        active_status_messages.pop(tid, None)
        stop_flags.pop(tid, None)

    # Antwort senden
    if tid not in active_processes: # Zusätzlicher Check ob nicht gerade ein neues /stop kam
        parts = [resp[i:i+4000] for i in range(0, len(resp), 4000)]
        for i, part in enumerate(parts):
            if i == 0:
                txt = f"✅ {part}" if not any(part.startswith(x) for x in ["✅", "❌", "⚠"]) else part
                try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
                    text=txt, parse_mode=ParseMode.MARKDOWN)
                except: 
                    try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
                        text=escape_html(txt), parse_mode=ParseMode.HTML)
                    except: pass
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=part, message_thread_id=tid)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("🤖 <b>Gemini Bot bereit.</b>", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("<b>Befehle:</b> /new, /list, /reset, /stop, /close, /reload", parse_mode=ParseMode.HTML)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.message.message_thread_id
    process = active_processes.get(tid)
    if process and process.returncode is None:
        stop_flags[tid] = True
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            await asyncio.sleep(0.5)
            if process.returncode is None: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except: pass
        msg = active_status_messages.pop(tid, None)
        if msg:
            try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text="🛑 <b>Abgebrochen.</b>", parse_mode=ParseMode.HTML)
            except: pass
    else: await update.message.reply_text("ℹ Keine aktive Aktion.")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.message.message_thread_id
    d = os.path.join(SESSIONS_BASE_DIR, f"topic_{tid or 'main'}", ".gemini", "tmp")
    if os.path.exists(d): shutil.rmtree(d)
    await update.message.reply_text("♻ Session gelöscht.", message_thread_id=tid)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Reload..."); save_config()
    with open(RELOAD_FILE, "w") as f: json.dump({"chat_id": update.effective_chat.id, "thread_id": update.message.message_thread_id}, f)
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "📂 <b>Projekte:</b>\n" + "\n".join([f"• {p['name']} ({p['thread_id']})" for p in config["projects"]])
    await update.message.reply_text(msg or "Keine Projekte.", parse_mode=ParseMode.HTML)

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.message.message_thread_id
    if not tid: return
    proj = next((p for p in config["projects"] if str(p.get("thread_id")) == str(tid)), None)
    if proj:
        config["projects"].remove(proj); save_config()
        shutil.rmtree(os.path.join(SESSIONS_BASE_DIR, f"topic_{tid}"), ignore_errors=True)
        try: await context.bot.delete_forum_topic(chat_id=update.effective_chat.id, message_thread_id=tid)
        except: pass

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_add[update.effective_user.id] = True
    await context.bot.send_message(chat_id=update.effective_chat.id, text="➕ Name für neues Projekt?", 
        reply_markup=ForceReply(selective=True), message_thread_id=update.message.message_thread_id)

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
