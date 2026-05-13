# 🎯 Contest Hunter Bot

Automatically detects low-engagement meme and art contests on Twitter/X and sends alerts directly to your Telegram.

No Twitter API key required — uses open-source Nitter scraping.

---

## Features

- Scans Twitter for meme contests, art contests, and giveaways
- Filters by engagement (only alerts on contests with few likes/retweets)
- Smart scoring — detects prize amounts, deadlines, submission signals
- Sends formatted Telegram DM alerts with tweet link
- Runs on a schedule, 24/7

---

## Quick Start

### 1. Clone the repo
```bash
git clone https://github.com/YOURNAME/contest-hunter.git
cd contest-hunter
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure credentials
```bash
cp .env.example .env
```
Edit `.env` and fill in your values (see Setup below).

### 4. Run
```bash
python contest_bot.py
```

---

## Setup — Get Your Telegram Credentials

**Create a bot:**
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the token → paste as `TELEGRAM_BOT_TOKEN` in `.env`

**Get your Chat ID:**
1. Send any message to your new bot
2. Open this URL in a browser (replace `TOKEN`):
   ```
   https://api.telegram.org/botTOKEN/getUpdates
   ```
3. Find `"chat":{"id": 123456789}` — that number is your `TELEGRAM_CHAT_ID`

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather | required |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID | required |
| `MAX_LIKES` | Max likes to consider "low engagement" | `50` |
| `MAX_RETWEETS` | Max retweets threshold | `20` |
| `CHECK_INTERVAL` | Minutes between scans | `20` |

---

## Deploy to Railway (Free)

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variables in the Railway dashboard (Variables tab)
5. Railway detects the `Procfile` and runs it as a background worker automatically

---

## Deploy to Render (Free)

1. Go to [render.com](https://render.com) → New → Background Worker
2. Connect your GitHub repo
3. Set Build Command: `pip install -r requirements.txt`
4. Set Start Command: `python contest_bot.py`
5. Add environment variables under the Environment tab

---

## Project Structure

```
contest-hunter/
├── contest_bot.py      ← main bot script
├── requirements.txt    ← Python dependencies
├── Procfile            ← tells Railway/Render how to run it
├── runtime.txt         ← pins Python version
├── .env.example        ← credential template (safe to commit)
├── .gitignore          ← keeps .env and logs out of git
└── README.md           ← this file
```

---

## How Detection Works

Each tweet is scored based on:
- **Keywords** — "meme contest", "art competition", "enter to win", etc.
- **Prize signals** — `$500`, `0.5 ETH`, "prize pool", "winner gets"
- **Action signals** — "submit by", "contest ends in", "like and RT to enter"

Tweets scoring 3+ that fall under your engagement thresholds trigger a Telegram alert.

---

## Sample Alert

```
🐸 MEME CONTEST — LOW ENGAGEMENT
━━━━━━━━━━━━━━━━━━━━
👤 @MemeLordSociety
❤️ 12 likes   🔁 4 retweets

📝 MEME BATTLE 🏆 Best meme wins 0.5 ETH!
Submit by replying below. Contest ends Friday...

🔍 Why detected:
  • keyword: meme battle
  • signal: 0.5 eth
  • signal: ends in friday

🔗 Open Tweet
⏰ 13 May 2026 · 14:32
```

---

## License

MIT
