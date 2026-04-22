import os
import json
import logging
import asyncio
import threading
from typing import Optional
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import stripe
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from openai import AsyncOpenAI
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "price_1TOYrbH9QKaRQPiwmvMXOla1")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

FREE_QUERIES_PER_DAY = 3

SYSTEM_PROMPT = f"""You are an elite business consultant with 20+ years of experience advising Fortune 500 companies and high-growth startups. You provide expert, actionable guidance on:

- Strategy: Market positioning, competitive analysis, growth levers, pivots, M&A
- Finance: Cash flow, unit economics, fundraising, valuations, runway management
- Operations: Process design, team structures, vendor selection, scaling
- Marketing & Sales: GTM strategy, pricing, customer acquisition, retention
- Product: Product-market fit, roadmaps, feature prioritization, launch strategy
- Legal & HR: Business structures, equity, compliance, hiring (general guidance only)
- AI Monetization: Building and monetizing AI-powered products, subscription models for AI tools, usage-based pricing, freemium strategies, deploying AI bots (Telegram, WhatsApp, web), integrating OpenAI/Claude APIs into paid products, reducing API costs, acquiring users for AI products, and scaling AI SaaS businesses

This bot operates on a freemium model. Free users get {FREE_QUERIES_PER_DAY} free queries per day. Pro users get unlimited queries. If a user asks about the limit, pricing, or how to get more queries, let them know they can upgrade to Pro for unlimited access using /upgrade.

Be direct and specific. Prioritize the 2-3 highest-leverage actions. Ask one clarifying question when context is insufficient. Always acknowledge tradeoffs."""


def get_user(chat_id: int) -> Optional[dict]:
    res = supabase.table("users").select("*").eq("chat_id", chat_id).execute()
    if not res.data:
        return None
    d = res.data[0]
    if isinstance(d["history"], str):
        d["history"] = json.loads(d["history"])
    return d


def upsert_user(chat_id: int, **kwargs):
    user = get_user(chat_id)
    if kwargs.get("history") is not None and not isinstance(kwargs["history"], str):
        kwargs["history"] = json.dumps(kwargs["history"])
    if not user:
        supabase.table("users").insert({"chat_id": chat_id, **kwargs}).execute()
    else:
        supabase.table("users").update(kwargs).eq("chat_id", chat_id).execute()


def check_and_increment_quota(chat_id: int) -> tuple[bool, int]:
    user = get_user(chat_id)
    if not user:
        upsert_user(chat_id)
        user = get_user(chat_id)

    if user["subscription_status"] == "active":
        return True, -1

    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_query_date"] != today:
        upsert_user(chat_id, queries_today=0, last_query_date=today)
        user["queries_today"] = 0

    if user["queries_today"] >= FREE_QUERIES_PER_DAY:
        return False, 0

    upsert_user(chat_id, queries_today=user["queries_today"] + 1, last_query_date=today)
    return True, FREE_QUERIES_PER_DAY - user["queries_today"] - 1


# FastAPI for Stripe webhook
web = FastAPI()

@web.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    bot = Bot(token=TELEGRAM_TOKEN)

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        chat_id = s["metadata"].get("chat_id")
        if chat_id:
            chat_id = int(chat_id)
            sub_id = s.get("subscription")
            updates = {
                "stripe_customer_id": s.get("customer"),
                "subscription_id": sub_id,
                "subscription_status": "active",
            }
            if sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                updates["subscription_start"] = datetime.utcfromtimestamp(sub["current_period_start"]).isoformat()
                updates["subscription_end"] = datetime.utcfromtimestamp(sub["current_period_end"]).isoformat()
            upsert_user(chat_id, **updates)
            await bot.send_message(chat_id=chat_id,
                                   text="✅ Payment successful! You're now on *Pro* — unlimited queries!",
                                   parse_mode="Markdown")

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        updates = {
            "subscription_status": "free",
            "subscription_end": datetime.utcfromtimestamp(sub["current_period_end"]).isoformat(),
        }
        supabase.table("users").update(updates).eq("subscription_id", sub["id"]).execute()
        res = supabase.table("users").select("chat_id").eq("subscription_id", sub["id"]).execute()
        if res.data:
            await bot.send_message(
                chat_id=res.data[0]["chat_id"],
                text=(
                    "😔 Your *Pro* subscription has ended.\n\n"
                    f"You're now back to *{FREE_QUERIES_PER_DAY} free queries per day*.\n\n"
                    "Use /upgrade anytime to resubscribe and get unlimited access again."
                ),
                parse_mode="Markdown",
            )

    elif event["type"] == "customer.subscription.updated":
        sub = event["data"]["object"]
        status = "active" if sub["status"] == "active" else "free"
        updates = {
            "subscription_status": status,
            "subscription_end": datetime.utcfromtimestamp(sub["current_period_end"]).isoformat(),
        }
        if status == "active":
            updates["subscription_start"] = datetime.utcfromtimestamp(sub["current_period_start"]).isoformat()
        supabase.table("users").update(updates).eq("subscription_id", sub["id"]).execute()

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            res = supabase.table("users").select("chat_id").eq("subscription_id", sub_id).execute()
            if res.data:
                attempt = invoice.get("attempt_count", 1)
                await bot.send_message(
                    chat_id=res.data[0]["chat_id"],
                    text=(
                        f"⚠️ *Payment failed* (attempt {attempt}).\n\n"
                        "We couldn't charge your card. Please update your payment method to keep your *Pro* access.\n\n"
                        "Use /upgrade to re-enter your payment details."
                    ),
                    parse_mode="Markdown",
                )

    return {"ok": True}


# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)

    if user and user["subscription_status"] == "active":
        await update.message.reply_text(
            "✅ You're on *Pro* — unlimited queries!\n\n"
            "Ask me anything about strategy, finance, marketing, operations, or fundraising.\n\n"
            "Commands:\n"
            "/reset — Clear conversation history\n"
            "/status — Check your plan\n"
            "/cancel — Cancel your subscription",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "👋 Welcome to *BizConsult AI* — your expert business advisor!\n\n"
            "Ask me anything about strategy, finance, marketing, operations, or fundraising.\n\n"
            f"You get *{FREE_QUERIES_PER_DAY} free queries per day*. Upgrade to Pro for unlimited access.\n\n"
            "Commands:\n"
            "/start — Welcome message\n"
            "/reset — Clear conversation history\n"
            "/status — Check your daily quota\n"
            "/upgrade — Upgrade to Pro (unlimited queries)",
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

    if user["subscription_status"] == "active":
        await update.message.reply_text(
            "📊 *Your Status*\n\n✅ Pro — Unlimited queries",
            parse_mode="Markdown",
        )
    else:
        remaining = max(0, FREE_QUERIES_PER_DAY - queries_today)
        await update.message.reply_text(
            f"📊 *Your Status*\n\n"
            f"Plan: Free\n"
            f"Queries used today: {queries_today}/{FREE_QUERIES_PER_DAY}\n"
            f"Remaining: {remaining}\n\n"
            f"Use /upgrade for unlimited access.",
            parse_mode="Markdown",
        )


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)

    if user and user["subscription_status"] == "active":
        await update.message.reply_text("✅ You're already on Pro — unlimited queries!")
        return

    if not stripe.api_key:
        await update.message.reply_text("Payment not configured yet. Please contact the owner.")
        return

    try:
        params = {
            "payment_method_types": ["card"],
            "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
            "mode": "subscription",
            "success_url": "https://t.me/" + (await context.bot.get_me()).username + "?start=success",
            "cancel_url": "https://t.me/" + (await context.bot.get_me()).username,
            "metadata": {"chat_id": str(chat_id)},
        }
        if user and user.get("stripe_customer_id"):
            params["customer"] = user["stripe_customer_id"]

        session = stripe.checkout.Session.create(**params)
        keyboard = [[InlineKeyboardButton("💳 Pay & Upgrade to Pro", url=session.url)]]
        await update.message.reply_text(
            "🚀 *Upgrade to Pro*\n\n"
            "Get unlimited queries for RM10/month.\n\n"
            "Click the button below to complete payment:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await update.message.reply_text(f"Error creating payment link: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)

    if not user or user["subscription_status"] != "active":
        await update.message.reply_text("You don't have an active Pro subscription.")
        return

    if not user.get("subscription_id"):
        await update.message.reply_text("No subscription found. Please contact support.")
        return

    try:
        sub = stripe.Subscription.modify(user["subscription_id"], cancel_at_period_end=True)
        end_date = datetime.utcfromtimestamp(sub["current_period_end"]).strftime("%d %b %Y")
        await update.message.reply_text(
            f"✅ Your subscription has been cancelled.\n\n"
            f"You'll keep *Pro* access until *{end_date}*, then revert to {FREE_QUERIES_PER_DAY} free queries/day.\n\n"
            "Changed your mind? Use /upgrade to resubscribe.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to cancel subscription: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    allowed, remaining = check_and_increment_quota(chat_id)
    if not allowed:
        keyboard = [[InlineKeyboardButton("💳 Upgrade to Pro", callback_data="upgrade")]]
        await update.message.reply_text(
            f"⚠️ You've used all {FREE_QUERIES_PER_DAY} free queries for today.\n\n"
            "Upgrade to Pro for unlimited access — RM10/month.",
            reply_markup=InlineKeyboardMarkup(keyboard),
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

    for i in range(0, len(reply), 4096):
        await update.message.reply_text(reply[i:i+4096])

    if remaining == 0:
        keyboard = [[InlineKeyboardButton("💳 Upgrade to Pro", callback_data="upgrade")]]
        await update.message.reply_text(
            "_That was your last free query today. Upgrade to Pro for unlimited access._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif remaining > 0:
        await update.message.reply_text(f"_{remaining} free queries remaining today_", parse_mode="Markdown")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "upgrade":
        await upgrade(update, context)


def run_web():
    uvicorn.run(web, host="0.0.0.0", port=8000, log_level="warning", loop="none")


def main():
    # Run FastAPI in background thread
    thread = threading.Thread(target=run_web, daemon=True)
    thread.daemon = True
    thread.start()
    import time; time.sleep(1)
    print("Webhook server running on port 8000")

    # Run Telegram bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, poll_interval=1)


if __name__ == "__main__":
    main()
