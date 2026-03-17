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
                logging.error(f"Sync error for {p['name']}: {e}")
                if "Not enough rights" in str(e):
                    await application.bot.send_message(chat_id=chat_id, text="❌ <b>Rechte fehlen:</b> Der Bot darf keine Topics erstellen.")
                    return
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

    async def update_status():
        nonlocal last_upd
        now = asyncio.get_event_loop().time()
        if (now - last_upd < 4): return 
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

    async def read_stdout():
        nonlocal current_tool
        while True:
            line = await process.stdout.readline()
            if not line: break
            try:
                data = json.loads(line.decode(errors='replace'))
                if data.get("type") == "message" and data.get("role") == "assistant":
                    full_resp.append(data.get("content", ""))
                elif data.get("type") == "tool_use": current_tool = data.get("tool_name")
                await update_status()
            except: pass

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line: break
            t = line.decode(errors='replace').strip()
            if t and not any(x in t for x in ["cached credentials", "YOLO mode"]):
                stderr_buffer.append(t)
                await update_status()

    await asyncio.gather(read_stdout(), read_stderr(), process.wait())
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
        os.makedirs(path, exist_ok=True)
        topic = await context.bot.create_forum_topic(chat_id=update.effective_chat.id, name=user_text)
        config["projects"].append({"path": path, "name": user_text, "thread_id": topic.message_thread_id})
        save_config()
        await context.bot.send_message(chat_id=update.effective_chat.id, message_thread_id=topic.message_thread_id,
                                       text=f"✅ Projekt <b>{escape_html(user_text)}</b> erstellt.", parse_mode=ParseMode.HTML)
        return

    if user_text == "📂 Liste": return await list_cmd(update, context)
    if user_text == "🆕 Neu": return await new_cmd(update, context)
    if user_text == "🛑 Stop": return await stop_cmd(update, context)
    if user_text == "♻️ Reset": return await reset_cmd(update, context)
    if user_text == "🗑 Close": return await close_cmd(update, context)
    if user_text == "➕ Hilfe": return await help_cmd(update, context)

    proj = get_active_project(tid)
    path = proj["path"] if proj else BOT_HOME
    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ Thinking...", message_thread_id=tid)

    cmd = ["gemini", "-r", "latest", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", user_text]
    resp, code, err = await run_gemini_command(cmd, path, update, context, status_msg, tid)
    if code != 0 and "No previous sessions found" in (resp + err):
        cmd = ["gemini", "--output-format", "stream-json", "--approval-mode", "yolo", "-p", user_text]
        resp, code, err = await run_gemini_command(cmd, path, update, context, status_msg, tid)

    if not resp: resp = "Done." if code == 0 else f"❌ Error ({code})\n<pre>{escape_html(err[-500:])}</pre>"
    
    parts = [resp[i:i+4000] for i in range(0, len(resp), 4000)]
    for i, part in enumerate(parts):
        if i == 0:
            try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part, parse_mode=ParseMode.MARKDOWN)
            except: 
                try: await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=part)
                except: pass
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=part, message_thread_id=tid)

# --- COMMANDS ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        await update.message.reply_text("🤖 <b>Bot ready.</b>", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ALLOWED_USER_ID:
        msg = "<b>Commands:</b> /new, /list, /reset, /stop, /close, /reload"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    pending_add[update.effective_user.id] = True
    await context.bot.send_message(chat_id=update.effective_chat.id, text="➕ Name?", reply_markup=ForceReply(selective=True), message_thread_id=update.message.message_thread_id)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    msg = "📂 <b>Projects:</b>\n" + "\n".join([f"• {p['name']} ({p['thread_id']})" for p in config["projects"]])
    await update.message.reply_text(msg or "None.", parse_mode=ParseMode.HTML)

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    tid = update.message.message_thread_id
    if not tid: return
    proj = next((p for p in config["projects"] if str(p["thread_id"]) == str(tid)), None)
    if proj:
        config["projects"].remove(proj); save_config()
        if os.path.exists(os.path.join(SESSIONS_BASE_DIR, f"topic_{tid}")): shutil.rmtree(os.path.join(SESSIONS_BASE_DIR, f"topic_{tid}"))
        try: await context.bot.delete_forum_topic(chat_id=update.effective_chat.id, message_thread_id=tid)
        except: pass

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    tid = update.message.message_thread_id
    if tid in active_processes:
        try: os.killpg(os.getpgid(active_processes[tid].pid), signal.SIGINT)
        except: pass

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    tid = update.message.message_thread_id
    d = os.path.join(SESSIONS_BASE_DIR, f"topic_{tid or 'main'}", ".gemini", "tmp")
    if os.path.exists(d): shutil.rmtree(d)
    await update.message.reply_text("♻ Reset.", message_thread_id=tid)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("🔄 Reloading...", message_thread_id=update.message.message_thread_id)
    with open(RELOAD_FILE, "w") as f: json.dump({"chat_id": update.effective_chat.id, "thread_id": update.message.message_thread_id}, f)
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
