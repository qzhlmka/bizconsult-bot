import os
import json
import sqlite3
from typing import Optional
from datetime import datetime
from contextlib import asynccontextmanager

import stripe
from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

FREE_QUERIES_PER_DAY = 100
PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

SYSTEM_PROMPT = """You are an elite business consultant with 20+ years of experience advising Fortune 500 companies and high-growth startups. You provide expert, actionable guidance on:

- **Strategy**: Market positioning, competitive analysis, growth levers, pivots, M&A
- **Finance**: Cash flow, unit economics, fundraising, valuations, runway management
- **Operations**: Process design, team structures, vendor selection, scaling
- **Marketing & Sales**: GTM strategy, pricing, customer acquisition, retention
- **Product**: Product-market fit, roadmaps, feature prioritization, launch strategy
- **Legal & HR**: Business structures, equity, compliance, hiring (general guidance only)

Style: Be direct and specific. Prioritize the 2-3 highest-leverage actions. Use frameworks when useful (SWOT, Porter's Five Forces, Jobs-to-be-Done) but never let frameworks replace judgment. Format responses clearly. Ask one clarifying question when context is insufficient. Always acknowledge tradeoffs."""

telegram_app: Application | None = None


def init_db():
    conn = sqlite3.connect("consultant.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            session_id TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            subscription_status TEXT DEFAULT 'free',
            subscription_id TEXT,
            queries_today INTEGER DEFAULT 0,
            last_query_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tg_history (
            chat_id INTEGER PRIMARY KEY,
            history TEXT DEFAULT '[]'
        )
    """)
    conn.commit()
    conn.close()


def get_user(session_id: str) -> Optional[dict]:
    conn = sqlite3.connect("consultant.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE session_id = ?", (session_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    cols = ["session_id", "stripe_customer_id", "subscription_status",
            "subscription_id", "queries_today", "last_query_date", "created_at"]
    return dict(zip(cols, row))


def upsert_user(session_id: str, **kwargs):
    conn = sqlite3.connect("consultant.db")
    conn.execute("INSERT OR IGNORE INTO users (session_id) VALUES (?)", (session_id,))
    for key, val in kwargs.items():
        conn.execute(f"UPDATE users SET {key} = ? WHERE session_id = ?", (val, session_id))
    conn.commit()
    conn.close()


def check_and_increment_quota(session_id: str) -> tuple[bool, int]:
    user = get_user(session_id)
    if not user:
        upsert_user(session_id)
        user = get_user(session_id)

    if user["subscription_status"] == "active":
        conn = sqlite3.connect("consultant.db")
        conn.execute("UPDATE users SET queries_today = queries_today + 1 WHERE session_id = ?",
                     (session_id,))
        conn.commit()
        conn.close()
        return True, -1

    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_query_date"] != today:
        upsert_user(session_id, queries_today=0, last_query_date=today)
        user["queries_today"] = 0

    if user["queries_today"] >= FREE_QUERIES_PER_DAY:
        return False, 0

    conn = sqlite3.connect("consultant.db")
    conn.execute(
        "UPDATE users SET queries_today = queries_today + 1, last_query_date = ? WHERE session_id = ?",
        (today, session_id),
    )
    conn.commit()
    conn.close()
    return True, FREE_QUERIES_PER_DAY - user["queries_today"] - 1


def get_tg_history(chat_id: int) -> list:
    conn = sqlite3.connect("consultant.db")
    c = conn.cursor()
    c.execute("SELECT history FROM tg_history WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return json.loads(row[0]) if row else []


def save_tg_history(chat_id: int, history: list):
    history = history[-20:]  # keep last 20 messages
    conn = sqlite3.connect("consultant.db")
    conn.execute("INSERT OR REPLACE INTO tg_history (chat_id, history) VALUES (?, ?)",
                 (chat_id, json.dumps(history)))
    conn.commit()
    conn.close()


async def ask_gpt(messages: list) -> str:
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.7,
        max_tokens=1024,
    )
    return response.choices[0].message.content


# Telegram handlers
async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *BizConsult AI* — your expert business advisor!\n\n"
        "Ask me anything about strategy, finance, marketing, operations, or fundraising.\n\n"
        "You have *100 free queries per day*. Type your question to get started.",
        parse_mode="Markdown",
    )


async def tg_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_tg_history(chat_id, [])
    await update.message.reply_text("Conversation reset. Start fresh!")


async def tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    session_id = f"tg_{chat_id}"

    allowed, remaining = check_and_increment_quota(session_id)
    if not allowed:
        await update.message.reply_text(
            f"You've used all {FREE_QUERIES_PER_DAY} free daily queries. "
            "Come back tomorrow or upgrade to Pro!"
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    history = get_tg_history(chat_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    try:
        reply = await ask_gpt(messages)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    save_tg_history(chat_id, history)

    # Telegram has a 4096 char limit per message
    for i in range(0, len(reply), 4096):
        await update.message.reply_text(reply[i:i+4096])

    if remaining >= 0:
        await update.message.reply_text(
            f"_{remaining} free queries remaining today_",
            parse_mode="Markdown",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    init_db()

    if TELEGRAM_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", tg_start))
        telegram_app.add_handler(CommandHandler("reset", tg_reset))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_message))
        await telegram_app.initialize()
        await telegram_app.start()
        print("Telegram bot started (webhook mode)")

    yield

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan, title="BizConsult AI")
app.mount("/static", StaticFiles(directory="static"), name="static")


class ChatRequest(BaseModel):
    message: str
    session_id: str
    history: list = []


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/api/chat")
async def chat(req: ChatRequest):
    allowed, remaining = check_and_increment_quota(req.session_id)

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "free_limit_reached",
                "message": f"You've used all {FREE_QUERIES_PER_DAY} free daily queries.",
            },
        )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in req.history[-12:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.message})

    async def generate():
        yield f"data: {json.dumps({'type': 'quota', 'remaining': remaining})}\n\n"
        try:
            stream = await client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                stream=True,
                temperature=0.7,
                max_tokens=4096,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield f"data: {json.dumps({'type': 'text', 'content': delta.content})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/status/{session_id}")
async def get_status(session_id: str):
    user = get_user(session_id)
    if not user:
        upsert_user(session_id)
        user = get_user(session_id)

    today = datetime.now().strftime("%Y-%m-%d")
    queries_today = user["queries_today"] if user["last_query_date"] == today else 0

    return {
        "subscription": user["subscription_status"],
        "queries_today": queries_today,
        "queries_remaining": (
            -1 if user["subscription_status"] == "active"
            else max(0, FREE_QUERIES_PER_DAY - queries_today)
        ),
    }


@app.post("/api/checkout")
async def create_checkout(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "")

    if not PRO_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe not configured — set STRIPE_PRO_PRICE_ID")

    user = get_user(session_id)
    params = {
        "payment_method_types": ["card"],
        "line_items": [{"price": PRO_PRICE_ID, "quantity": 1}],
        "mode": "subscription",
        "success_url": str(request.base_url).rstrip("/") + "/?success=true&session_id=" + session_id,
        "cancel_url": str(request.base_url).rstrip("/") + "/?canceled=true",
        "metadata": {"session_id": session_id},
    }
    if user and user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]

    checkout = stripe.checkout.Session.create(**params)
    return {"url": checkout.url}


@app.post("/api/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    etype = event["type"]

    if etype == "checkout.session.completed":
        s = event["data"]["object"]
        sid = s["metadata"].get("session_id")
        if sid:
            upsert_user(sid,
                        stripe_customer_id=s.get("customer"),
                        subscription_id=s.get("subscription"),
                        subscription_status="active")

    elif etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub = event["data"]["object"]
        status = "active" if sub["status"] == "active" else "free"
        conn = sqlite3.connect("consultant.db")
        conn.execute("UPDATE users SET subscription_status = ? WHERE subscription_id = ?",
                     (status, sub["id"]))
        conn.commit()
        conn.close()

    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Telegram not configured")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/telegram/setup")
async def telegram_setup(request: Request):
    """Call this once to register the webhook URL with Telegram."""
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Set TELEGRAM_BOT_TOKEN first")
    webhook_url = str(request.base_url).rstrip("/") + "/telegram/webhook"
    await telegram_app.bot.set_webhook(webhook_url)
    return {"ok": True, "webhook": webhook_url}
