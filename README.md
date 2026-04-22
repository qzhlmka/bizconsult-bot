# BizConsult AI — Telegram Business Consultant Bot

An AI-powered business consultant chatbot that runs on Telegram, powered by OpenAI GPT-4o. Users can ask questions about business strategy, finance, marketing, operations, and more — and get expert, actionable advice instantly.

## What It Does

- Answers business questions via Telegram chat
- Remembers conversation history per user (last 20 messages)
- Freemium model — 10 free queries/day, unlimited for Pro users
- Stripe payment integration — RM10/month subscription
- Resets the query count automatically every day
- Stores all data in Supabase (PostgreSQL cloud database)

---

## How It Works

### Architecture

The entire bot runs as a single Python process (`bot.py`) with two concurrent components:

1. **Telegram Bot** — uses `python-telegram-bot` in polling mode. It continuously asks Telegram's servers for new messages and handles them with registered command and message handlers.
2. **FastAPI Webhook Server** — runs on port 8000 in a background thread. Stripe sends HTTP POST requests here whenever a payment event occurs (e.g. subscription created, payment failed).

Both components share the same process and the same Supabase connection, which is how the webhook server can update user records and the bot can read them in real time.

---

### Message Flow

When a user sends a message:

1. Telegram delivers it to the bot via polling
2. `check_and_increment_quota()` is called — checks the user's `subscription_status` in Supabase
   - If `active` → skip quota, allow the query
   - If `free` → check `queries_today` against the daily limit
     - If `last_query_date` is not today → reset `queries_today` to 0 first (daily reset)
     - If limit reached → block and show upgrade prompt
     - Otherwise → increment `queries_today` by 1
3. The last 12 messages from the user's `history` column are retrieved and passed as context to GPT-4o
4. GPT-4o returns a reply, which is sent back to the user
5. The new message pair (user + assistant) is appended to `history` and saved (capped at 20 messages)

---

### Quota System

- Free users get **10 queries per day**
- The counter `queries_today` is stored in Supabase per user
- The reset is lazy — it only resets when the user sends their next message on a new day, by comparing `last_query_date` to today's date
- Pro users bypass the quota entirely — `subscription_status = "active"` skips all quota checks

---

### Payment Flow (Stripe)

When a user runs `/upgrade`:

1. Bot creates a Stripe Checkout Session with the user's `chat_id` stored in the session metadata
2. User clicks the payment link and enters their card details on Stripe's hosted checkout page
3. Stripe charges the card and fires `checkout.session.completed` to your `/webhook` endpoint
4. The webhook reads `chat_id` from the metadata, retrieves the subscription object to get `current_period_start` and `current_period_end`, then updates Supabase:
   - `subscription_status = "active"`
   - `stripe_customer_id` — saved for future reference
   - `subscription_id` — used to match all future webhook events to this user
   - `subscription_start` / `subscription_end` — billing period dates
5. Bot sends the user a confirmation message in Telegram

From that point, **Stripe automatically charges the card every month** — the user does nothing. On each successful renewal, `customer.subscription.updated` fires and the bot updates `subscription_start` / `subscription_end` in Supabase.

---

### Cancellation Flow

When a user runs `/cancel`:

1. Bot calls `stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)`
2. Stripe marks the subscription to cancel at the end of the current billing period — the user keeps Pro access until then
3. When the period ends, Stripe fires `customer.subscription.deleted`
4. The webhook updates Supabase: `subscription_status = "free"`, `queries_today = 0`, `subscription_end = ended_at`
5. Bot sends the user a Telegram message notifying them they've been downgraded

---

### Payment Failure Flow

If Stripe fails to charge the card on renewal:

1. Stripe fires `invoice.payment_failed` with the attempt count
2. The webhook looks up the user by `subscription_id` and sends a warning message in Telegram with the attempt number
3. Stripe automatically retries (3–4 times over ~2 weeks, configurable in Stripe dashboard under **Billing → Smart Retries**)
4. If all retries fail, Stripe cancels the subscription and fires `customer.subscription.deleted` → user is downgraded to free

---

### Database Schema (Supabase)

Table: `users`

| Column | Type | Description |
|--------|------|-------------|
| `chat_id` | bigint (PK) | Telegram user ID |
| `queries_today` | integer | Queries used today (resets daily) |
| `last_query_date` | text | Date of last query (`YYYY-MM-DD`), used to trigger daily reset |
| `history` | text | JSON array of last 20 messages (role + content) |
| `subscription_status` | text | `"free"` or `"active"` |
| `stripe_customer_id` | text | Stripe customer ID |
| `subscription_id` | text | Stripe subscription ID — used to match webhook events to users |
| `subscription_start` | timestamptz | When the current billing period started |
| `subscription_end` | timestamptz | When the current billing period ends (updates on renewal) |
| `created_at` | timestamp | When the user first used the bot |

---

## File Structure

```
consultant_bot/
├── bot.py              # Main bot logic — Telegram, OpenAI, Stripe webhook, Supabase
├── requirements.txt    # Python dependencies
├── Procfile            # Tells Railway how to run the bot (worker: python bot.py)
├── .env.example        # Template for environment variables
└── .gitignore          # Excludes .env and cache files from Git
```

---

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
- `STRIPE_WEBHOOK_SECRET` — from Stripe CLI (`stripe listen`) for local, or from Stripe dashboard for production
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

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and instructions |
| `/reset` | Clear your conversation history |
| `/status` | Check your plan and daily quota |
| `/upgrade` | Upgrade to Pro — unlimited queries for RM10/month |
| `/cancel` | Cancel your Pro subscription (access kept until period ends) |

---

## Stripe Webhook Events

The bot listens for these Stripe events at `POST /webhook`. Each event is verified using the `STRIPE_WEBHOOK_SECRET` before being processed.

| Event | What Stripe sends it | What the bot does |
|-------|----------------------|-------------------|
| `checkout.session.completed` | User completes payment | Activates Pro, saves `subscription_id`, `stripe_customer_id`, `subscription_start`, `subscription_end` |
| `customer.subscription.updated` | Subscription renews each month | Updates `subscription_start` and `subscription_end` in Supabase |
| `customer.subscription.deleted` | Subscription cancelled or all payment retries failed | Sets `subscription_status = "free"`, resets `queries_today = 0`, notifies user via Telegram |
| `invoice.payment_failed` | Card charge fails on renewal | Sends warning to user via Telegram with retry attempt number |

---

## Deploying to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add all environment variables in the Railway dashboard
4. Deploy — Railway runs the bot 24/7
5. Go to Stripe Dashboard → **Developers** → **Webhooks** → **Add endpoint**
   - URL: `https://your-app.railway.app/webhook`
   - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
6. Copy the webhook signing secret (starts with `whsec_`) into Railway as `STRIPE_WEBHOOK_SECRET` — this is different from the one the local CLI generates

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from BotFather |
| `STRIPE_SECRET_KEY` | Your Stripe secret key |
| `STRIPE_PRICE_ID` | Your Stripe subscription price ID |
| `STRIPE_WEBHOOK_SECRET` | Webhook signing secret from Stripe CLI (local) or Stripe dashboard (production) |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon key |
