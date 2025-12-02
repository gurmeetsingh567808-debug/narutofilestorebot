import os
import sqlite3
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
import asyncio

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
GROUP_ID = int(os.getenv("GROUP_ID"))
OWNER_ID = int(os.getenv("OWNER_ID"))

conn = sqlite3.connect("store.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS pending (user_id INTEGER, message_id INTEGER)")
conn.commit()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Send a file and then use filestore")


async def filestore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = cur.execute("SELECT message_id FROM pending WHERE user_id=?",
                          (update.message.from_user.id,)).fetchone()
    if not pending:
        return await update.message.reply_text("No file received!")

    file_msg_id = pending[0]

    await asyncio.sleep(1.5)

    sent = await context.bot.forward_message(
        chat_id=GROUP_ID,
        from_chat_id=update.message.chat_id,
        message_id=file_msg_id
    )

    file_id = sent.message_id
    link = f"https://t.me/{BOT_USERNAME}?start={file_id}"

    await update.message.reply_text(f"Stored!\nYour link:\n{link}")

    cur.execute("DELETE FROM pending WHERE user_id=?", (update.message.from_user.id,))
    conn.commit()


async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document or update.message.photo or update.message.video:
        cur.execute("DELETE FROM pending WHERE user_id=?", (update.message.from_user.id,))
        cur.execute("INSERT INTO pending VALUES(?,?)",
                    (update.message.from_user.id, update.message.message_id))
        conn.commit()


async def getfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file_msg_id = int(context.args[0])
    except:
        return await update.message.reply_text("Invalid link")

    await context.bot.forward_message(
        chat_id=update.message.chat_id,
        from_chat_id=GROUP_ID,
        message_id=file_msg_id
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("filestore", filestore))
    app.add_handler(CommandHandler("get", getfile))
    app.add_handler(MessageHandler(filters.ALL, capture))

    print("BOT running...")
    app.run_polling()


if __name__ == "__main__":
    main()