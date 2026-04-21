import os
import json
import sqlite3
import logging
from typing import Optional
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

FREE_QUERIES_PER_DAY = 100

SYSTEM_PROMPT = f"""You are an elite business consultant with 20+ years of experience advising Fortune 500 companies and high-growth startups. You provide expert, actionable guidance on:

- Strategy: Market positioning, competitive analysis, growth levers, pivots, M&A
- Finance: Cash flow, unit economics, fundraising, valuations, runway management
- Operations: Process design, team structures, vendor selection, scaling
- Marketing & Sales: GTM strategy, pricing, customer acquisition, retention
- Product: Product-market fit, roadmaps, feature prioritization, launch strategy
- Legal & HR: Business structures, equity, compliance, hiring (general guidance only)
- AI Monetization: Building and monetizing AI-powered products, subscription models for AI tools, usage-based pricing, freemium strategies, deploying AI bots (Telegram, WhatsApp, web), integrating OpenAI/Claude APIs into paid products, reducing API costs, acquiring users for AI products, and scaling AI SaaS businesses

This bot operates on a freemium model. Users get {FREE_QUERIES_PER_DAY} free queries per day. Once they hit the limit, they are informed they need to upgrade to continue. If a user asks about the limit, pricing, or how to get more queries, let them know they get {FREE_QUERIES_PER_DAY} free queries per day and to contact the bot owner to upgrade.

Be direct and specific. Prioritize the 2-3 highest-leverage actions. Ask one clarifying question when context is insufficient. Always acknowledge tradeoffs."""


def init_db():
    conn = sqlite3.connect("consultant.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            queries_today INTEGER DEFAULT 0,
            last_query_date TEXT,
            history TEXT DEFAULT '[]'
        )
    """)
    conn.commit()
    conn.close()


def get_user(chat_id: int) -> Optional[dict]:
    conn = sqlite3.connect("consultant.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {"chat_id": row[0], "queries_today": row[1], "last_query_date": row[2], "history": json.loads(row[3])}


def upsert_user(chat_id: int, **kwargs):
    conn = sqlite3.connect("consultant.db")
    conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    for key, val in kwargs.items():
        if key == "history":
            val = json.dumps(val)
        conn.execute(f"UPDATE users SET {key} = ? WHERE chat_id = ?", (val, chat_id))
    conn.commit()
    conn.close()


def check_and_increment_quota(chat_id: int) -> tuple[bool, int]:
    user = get_user(chat_id)
    if not user:
        upsert_user(chat_id)
        user = get_user(chat_id)

    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_query_date"] != today:
        upsert_user(chat_id, queries_today=0, last_query_date=today)
        user["queries_today"] = 0

    if user["queries_today"] >= FREE_QUERIES_PER_DAY:
        return False, 0

    upsert_user(chat_id, queries_today=user["queries_today"] + 1, last_query_date=today)
    return True, FREE_QUERIES_PER_DAY - user["queries_today"] - 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *BizConsult AI* — your expert business advisor!\n\n"
        "Ask me anything about strategy, finance, marketing, operations, or fundraising.\n\n"
        f"You get *{FREE_QUERIES_PER_DAY} free queries per day*.\n\n"
        "Commands:\n"
        "/start — Welcome message\n"
        "/reset — Clear conversation history\n"
        "/status — Check your daily quota",
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    upsert_user(chat_id, history=[])
    await update.message.reply_text("Conversation cleared. Fresh start!")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if not user:
        upsert_user(chat_id)
        user = get_user(chat_id)

    today = datetime.now().strftime("%Y-%m-%d")
    queries_today = user["queries_today"] if user["last_query_date"] == today else 0
    remaining = max(0, FREE_QUERIES_PER_DAY - queries_today)

    await update.message.reply_text(
        f"📊 *Your Usage Today*\n\n"
        f"Queries used: {queries_today}/{FREE_QUERIES_PER_DAY}\n"
        f"Remaining: {remaining}",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    allowed, remaining = check_and_increment_quota(chat_id)
    if not allowed:
        await update.message.reply_text(
            f"⚠️ You've used all {FREE_QUERIES_PER_DAY} free queries for today.\n"
            "Come back tomorrow for more!"
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    user = get_user(chat_id)
    history = user["history"] if user else []

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-12:])
    messages.append({"role": "user", "content": text})

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=1024,
        )
        reply = response.choices[0].message.content
    except Exception as e:
        await update.message.reply_text(f"Something went wrong: {e}")
        return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    upsert_user(chat_id, history=history[-20:])

    # Split if reply exceeds Telegram's 4096 char limit
    for i in range(0, len(reply), 4096):
        await update.message.reply_text(reply[i:i+4096])

    if remaining > 0:
        await update.message.reply_text(f"_{remaining} queries remaining today_", parse_mode="Markdown")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, poll_interval=1)


if __name__ == "__main__":
    main()
