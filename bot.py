#!/usr/bin/env python3
# Final PTB v20 filestore bot — full features (silent batch, single filestore, restore, admin)
import os
import asyncio
import logging
import sqlite3
import random
import string
from time import time
from datetime import datetime
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG (via env) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "YourBotUsername")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BACKUP_GROUP_ID = int(os.getenv("BACKUP_GROUP_ID") or 0) or None
AUTO_DELETE = int(os.getenv("AUTO_DELETE") or 0)  # seconds; 0 = disabled

# ---------------- logging ----------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- DB init ----------------
DB_FILE = "filestore.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# files: single-file codes (code -> message_id in GROUP)
cur.execute("""
CREATE TABLE IF NOT EXISTS files (
    code TEXT PRIMARY KEY,
    msg_id INTEGER,
    owner INTEGER,
    created_at INTEGER,
    caption TEXT,
    file_type TEXT
)
""")

# batches: batch metadata
cur.execute("""
CREATE TABLE IF NOT EXISTS batches (
    code TEXT PRIMARY KEY,
    owner INTEGER,
    created_at INTEGER,
    item_count INTEGER
)
""")

# items: mapping batch_code -> group message ids (ordered by rowid)
cur.execute("""
CREATE TABLE IF NOT EXISTS items (
    code TEXT,
    msg_id INTEGER,
    owner INTEGER
)
""")

# admins
cur.execute("""
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY
)
""")

# meta
cur.execute("""
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
)
""")

conn.commit()

# ensure owner is admin
cur.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (OWNER_ID,))
conn.commit()

# ---------------- in-memory modes ----------------
filestore_mode = {}   # user_id -> True (only next file)
batch_mode = {}       # user_id -> [msg_id, msg_id, ...] (silent)

# ---------------- utilities ----------------
def gen_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_admin(uid: int) -> bool:
    r = cur.execute("SELECT 1 FROM admins WHERE id=?", (uid,)).fetchone()
    return bool(r)

async def try_forward(msg, target_chat_id, retries=3, delay=0.5):
    """Forward the given incoming message to target_chat_id; retry on failure.
       Returns forwarded message_id or None."""
    for attempt in range(retries):
        try:
            forwarded = await msg.forward(int(target_chat_id))
            return forwarded.message_id
        except Exception as e:
            logger.warning(f"Forward attempt {attempt+1} failed: {e}")
            await asyncio.sleep(delay)
    return None

async def forward_message_by_id(app, from_chat_id, message_id, to_chat_id):
    """Forward a message already in a chat to another chat."""
    try:
        res = await app.bot.forward_message(chat_id=int(to_chat_id),
                                            from_chat_id=int(from_chat_id),
                                            message_id=int(message_id))
        return res
    except Exception as e:
        logger.exception("forward_message_by_id failed: %s", e)
        return None

# ---------------- COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handle /start CODE deep-link (args)
    args = context.args or []
    if len(args) >= 1:
        code = args[0].strip()
        return await handle_restore_request(update, context, code)

    # normal start
    await update.message.reply_text(
        "Welcome — Filestore Bot.\n"
        "Use /help to see commands.\n\n"
        "filestore -> store the next file you send (one file)\n"
        "batch -> start silent batch (admin only)\n"
        "batchdone -> finish batch and get link"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "filestore – store the next message (one file)\n"
        "myfiles – list your stored codes\n"
        "setcode NEWCODE – rename your last stored file\n\n"
        "batch – start silent batch (admin only)\n"
        "batchdone – finish batch and get link\n\n"
        "stats – admin only\n"
        "adminlist – admin only\n"
        "addadmin USERID – owner only\n"
        "removeadmin USERID – owner only\n"
    )

# filestore: enable only-next-file mode
async def cmd_filestore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    filestore_mode[uid] = True
    await update.message.reply_text("Send the file/message you want to store (single file).")

# list user's files
async def cmd_myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = cur.execute("SELECT code, created_at FROM files WHERE owner=? ORDER BY created_at DESC", (uid,)).fetchall()
    if not rows:
        return await update.message.reply_text("You have no stored files.")
    out = []
    for code, ts in rows:
        dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"{code} — {dt}\nhttps://t.me/{BOT_USERNAME}?start={code}")
    await update.message.reply_text("\n\n".join(out))

# rename last stored code
async def cmd_setcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: setcode NEWCODE")
    new_code = context.args[0].strip()
    # check collision
    if cur.execute("SELECT 1 FROM files WHERE code=?", (new_code,)).fetchone() or \
       cur.execute("SELECT 1 FROM batches WHERE code=?", (new_code,)).fetchone():
        return await update.message.reply_text("Code already in use.")
    row = cur.execute("SELECT code FROM files WHERE owner=? ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
    if not row:
        return await update.message.reply_text("No recent file to rename.")
    old = row[0]
    cur.execute("UPDATE files SET code=? WHERE code=?", (new_code, old))
    conn.commit()
    await update.message.reply_text(f"Code updated: https://t.me/{BOT_USERNAME}?start={new_code}")

# ---------------- Admin commands ----------------
# start batch (silent) — admin only; intentionally silent (no reply)
async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    batch_mode[uid] = []
    # silent start: do not reply

# finish batch: store items into DB and give single code
async def cmd_batchdone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    if uid not in batch_mode:
        return await update.message.reply_text("No active batch.")
    items = batch_mode.pop(uid)
    if not items:
        return await update.message.reply_text("Batch is empty.")
    code = gen_code(8)
    now_ts = int(time())
    cur.execute("INSERT INTO batches VALUES(?,?,?,?)", (code, uid, now_ts, len(items)))
    # store items preserving order
    for mid in items:
        cur.execute("INSERT INTO items VALUES(?,?,?)", (code, mid, uid))
    conn.commit()
    await update.message.reply_text(f"Batch saved!\nhttps://t.me/{BOT_USERNAME}?start={code}")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    total_files = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_batches = cur.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    total_items = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_admins = cur.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    await update.message.reply_text(
        f"Files: {total_files}\nBatches: {total_batches}\nItems: {total_items}\nAdmins: {total_admins}"
    )

async def cmd_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = cur.execute("SELECT id FROM admins").fetchall()
    await update.message.reply_text("Admins:\n" + "\n".join(str(r[0]) for r in rows))

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: addadmin USERID")
    uid = int(context.args[0])
    cur.execute("INSERT OR IGNORE INTO admins VALUES(?)", (uid,))
    conn.commit()
    await update.message.reply_text(f"Added admin {uid}")

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: removeadmin USERID")
    uid = int(context.args[0])
    cur.execute("DELETE FROM admins WHERE id=?", (uid,))
    conn.commit()
    await update.message.reply_text(f"Removed admin {uid}")

# ---------------- message handler ----------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    uid = msg.from_user.id

    # ignore commands
    if msg.text and msg.text.startswith("/"):
        return

    # 1) filestore mode (only next file)
    if uid in filestore_mode:
        filestore_mode.pop(uid, None)
        # forward to GROUP
        mid = await try_forward(msg, GROUP_ID)
        if not mid:
            return await msg.reply_text("❌ Could not store the file (forward failed).")
        # store single file record
        code = gen_code(8)
        cur.execute("INSERT INTO files VALUES(?,?,?,?,?,?)",
                    (code, mid, uid, int(time()), msg.caption or "", detect_file_type(msg)))
        conn.commit()
        await msg.reply_text(f"Stored!\nhttps://t.me/{BOT_USERNAME}?start={code}")
        return

    # 2) batch mode (silent): forward and append msg_id, no reply
    if uid in batch_mode:
        mid = await try_forward(msg, GROUP_ID)
        if mid:
            batch_mode[uid].append(mid)
        return

    # 3) normal mode: ignore everything
    return

# ---------------- restore (deep link) ----------------
async def handle_restore_request(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    chat_id = update.effective_chat.id

    # check single file
    row = cur.execute("SELECT msg_id FROM files WHERE code=?", (code,)).fetchone()
    if row:
        mid = row[0]
        forwarded = await forward_message_by_id(context.application, GROUP_ID, mid, chat_id)
        if forwarded:
            return
        else:
            return await update.message.reply_text("❌ Failed to restore file.")

    # check batch items
    rows = cur.execute("SELECT msg_id FROM items WHERE code=? ORDER BY rowid ASC", (code,)).fetchall()
    if rows:
        await update.message.reply_text(f"Sending {len(rows)} files…")
        for (mid,) in rows:
            await forward_message_by_id(context.application, GROUP_ID, mid, chat_id)
            await asyncio.sleep(1.5)
        return

    return await update.message.reply_text("Invalid or expired link.")

# ---------------- helper to detect file type ----------------
def detect_file_type(msg):
    if msg.document:
        return "document"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.sticker:
        return "sticker"
    return "unknown"

# ---------------- auto-delete loop ----------------
async def auto_delete_loop(app):
    while True:
        row = cur.execute("SELECT v FROM meta WHERE k='auto_delete_enabled'").fetchone()
        enabled = row and row[0] == "1"
        if not enabled:
            await asyncio.sleep(10)
            continue
        row2 = cur.execute("SELECT v FROM meta WHERE k='auto_delete_seconds'").fetchone()
        delay = int(row2[0]) if row2 else AUTO_DELETE
        if not delay:
            await asyncio.sleep(10)
            continue
        now_ts = int(time())
        # files
        for code, created in cur.execute("SELECT code, created_at FROM files").fetchall():
            if now_ts - created > delay:
                cur.execute("DELETE FROM files WHERE code=?", (code,))
        # batches & items older than delay (based on batch created_at)
        for code, created, _owner, _count in cur.execute("SELECT code, created_at, owner, item_count FROM batches").fetchall():
            if now_ts - created > delay:
                cur.execute("DELETE FROM batches WHERE code=?", (code,))
                cur.execute("DELETE FROM items WHERE code=?", (code,))
        conn.commit()
        await asyncio.sleep(30)

# ---------------- post_init ----------------
async def post_init(app):
    # set default meta if not exists
    cur.execute("INSERT OR IGNORE INTO meta VALUES('auto_delete_enabled','0')")
    cur.execute("INSERT OR IGNORE INTO meta VALUES('auto_delete_seconds',?)", (AUTO_DELETE,))
    conn.commit()

    # set commands in bot menu
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Start the bot / restore file"),
            BotCommand("help", "Show help"),
            BotCommand("filestore", "Store the next file you send"),
            BotCommand("myfiles", "List your stored files"),
            BotCommand("setcode", "Rename last stored file"),
            BotCommand("batch", "Start batch mode (admin)"),
            BotCommand("batchdone", "Finish batch and generate one link"),
            BotCommand("stats", "Show bot stats (admin)"),
            BotCommand("adminlist", "List admins"),
            BotCommand("addadmin", "Add an admin (owner only)"),
            BotCommand("removeadmin", "Remove an admin (owner only)")
        ])
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)

    # start auto-delete loop if enabled
    asyncio.create_task(auto_delete_loop(app))
    logger.info("Post init complete.")

# ---------------- main ----------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables.")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("filestore", cmd_filestore))
    app.add_handler(CommandHandler("myfiles", cmd_myfiles))
    app.add_handler(CommandHandler("setcode", cmd_setcode))
    app.add_handler(CommandHandler("batch", cmd_batch))
    app.add_handler(CommandHandler("batchdone", cmd_batchdone))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("adminlist", cmd_adminlist))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))

    # messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("🔥 Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()