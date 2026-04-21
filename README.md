# BizConsult AI — Telegram Business Consultant Bot

An AI-powered business consultant chatbot that runs on Telegram, powered by OpenAI GPT-4o. Users can ask questions about business strategy, finance, marketing, operations, and more — and get expert, actionable advice instantly.

## What It Does

- Answers business questions via Telegram chat
- Remembers conversation history per user (last 20 messages)
- Limits free users to 100 queries per day
- Resets the query count automatically every day
- Stores all data locally in a SQLite database

## Features

- **GPT-4o powered** — intelligent, context-aware business advice
- **Conversation memory** — remembers the context of your chat
- **Daily quota** — 100 free queries per user per day
- **Simple commands** — `/start`, `/reset`, `/status`

## File Structure

```
consultant_bot/
├── bot.py              # Main bot logic — handles Telegram messages, quota tracking, and OpenAI calls
├── requirements.txt    # Python dependencies
├── Procfile            # Tells Railway how to run the bot (worker: python bot.py)
├── .env.example        # Template for environment variables (copy to .env and fill in your keys)
├── .gitignore          # Excludes .env, database, and cache files from Git
└── static/
    └── index.html      # Web UI (not used in Telegram-only mode)
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

### 4. Run locally
```bash
OPENAI_API_KEY=your-key TELEGRAM_BOT_TOKEN=your-token python bot.py
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and instructions |
| `/reset` | Clear your conversation history |
| `/status` | Check how many queries you have left today |

## Deploying to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables (`OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`) in the Railway dashboard
4. Set start command to `python bot.py`
5. Deploy — your bot runs 24/7

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from BotFather |
