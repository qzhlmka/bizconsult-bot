# BizConsult AI — Telegram Business Consultant Bot

An AI-powered business consultant chatbot that runs on Telegram, powered by OpenAI GPT-4o. Users can ask questions about business strategy, finance, marketing, operations, and more — and get expert, actionable advice instantly.

## What It Does

- Answers business questions via Telegram chat
- Remembers conversation history per user (last 20 messages)
- Freemium model — 10 free queries/day, unlimited for Pro users
- Stripe payment integration — RM10/month subscription
- Resets the query count automatically every day
- Stores all data in Supabase (PostgreSQL cloud database)

## Features

- **GPT-4o powered** — intelligent, context-aware business advice
- **Conversation memory** — remembers the context of your chat
- **Daily quota** — 10 free queries per user per day
- **Stripe payments** — users can upgrade to Pro via `/upgrade`
- **Supabase database** — cloud storage for users and subscriptions
- **Simple commands** — `/start`, `/reset`, `/status`, `/upgrade`, `/cancel`
- **Subscription lifecycle** — auto-notifies users on payment failure, cancellation, and plan expiry

## File Structure

```
consultant_bot/
├── bot.py              # Main bot logic — Telegram, OpenAI, Stripe webhook, Supabase
├── requirements.txt    # Python dependencies
├── Procfile            # Tells Railway how to run the bot (worker: python bot.py)
├── .env.example        # Template for environment variables
└── .gitignore          # Excludes .env and cache files from Git
```

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/qzhlmka/bizconsult-bot.git
cd bizconsult-bot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Set environment variables
```bash
cp .env.example .env
```
Fill in your keys in `.env`:
- `OPENAI_API_KEY` — get from [platform.openai.com](https://platform.openai.com)
- `TELEGRAM_BOT_TOKEN` — get from [@BotFather](https://t.me/BotFather) on Telegram
- `STRIPE_SECRET_KEY` — get from [dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys)
- `STRIPE_PRICE_ID` — your subscription price ID from Stripe dashboard
- `STRIPE_WEBHOOK_SECRET` — from Stripe CLI (`stripe listen`)
- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_KEY` — your Supabase anon key

### 4. Run locally
```bash
export $(cat .env | xargs) && python bot.py
```

### 5. Forward Stripe webhooks locally (in a second terminal)
```bash
stripe listen --forward-to localhost:8000/webhook
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and instructions |
| `/reset` | Clear your conversation history |
| `/status` | Check your plan and daily quota |
| `/upgrade` | Upgrade to Pro — unlimited queries for RM10/month |
| `/cancel` | Cancel your Pro subscription (access kept until period ends) |

## Stripe Webhook Events

The bot listens for these Stripe events at `/webhook`:

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Activates Pro, saves subscription dates |
| `customer.subscription.updated` | Updates subscription start/end dates on renewal |
| `customer.subscription.deleted` | Downgrades user to free, notifies via Telegram |
| `invoice.payment_failed` | Warns user via Telegram with retry count |

## Deploying to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add all environment variables in the Railway dashboard
4. Deploy — your bot runs 24/7
5. Go to Stripe Dashboard → **Developers** → **Webhooks** → **Add endpoint**
   - URL: `https://your-app.railway.app/webhook`
   - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
6. Copy the webhook signing secret into Railway as `STRIPE_WEBHOOK_SECRET`

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from BotFather |
| `STRIPE_SECRET_KEY` | Your Stripe secret key |
| `STRIPE_PRICE_ID` | Your Stripe subscription price ID |
| `STRIPE_WEBHOOK_SECRET` | Webhook secret from Stripe CLI or dashboard |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon key |
