"""
╔══════════════════════════════════════════════════════════════╗
║        CONTEST HUNTER BOT — Interactive Telegram Version     ║
║   Scrapes Twitter/X → Alerts subscribers via Telegram        ║
╚══════════════════════════════════════════════════════════════╝

USER COMMANDS:
    /start              → Subscribe to contest alerts
    /stop               → Pause your alerts
    /status             → Check subscription + last scan info
    /setfilter meme     → Only get meme contest alerts
    /setfilter art      → Only get art contest alerts
    /setfilter video    → Only get video contest alerts
    /setfilter all      → Get all contest types (default)
    /threshold 30 10    → Set max likes / max retweets
    /scan               → Trigger an instant manual scan
    /help               → Show all commands

SETUP:
    pip install ntscraper python-telegram-bot python-dotenv schedule

.env file:
    TELEGRAM_BOT_TOKEN=8883003786:AAFOTbzX_keof_UE6aW4jG0sPom28UTTVik
    CHECK_INTERVAL=20
"""

import os
import re
import json
import time
import logging
import schedule
import asyncio
import threading
from datetime import datetime
from dotenv import load_dotenv

try:
    from ntscraper import Nitter
except ImportError:
    print("Run: pip install ntscraper python-telegram-bot python-dotenv schedule")
    exit(1)

try:
    from telegram import Update, BotCommand
    from telegram.ext import (
        Application, CommandHandler, ContextTypes
    )
    from telegram.constants import ParseMode
except ImportError:
    print("Run: pip install python-telegram-bot")
    exit(1)

# ── Load Config ───────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", 20))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("contest_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Subscriber Storage ────────────────────────────────────────────────────────
# subscribers.json structure:
# {
#   "chat_id": {
#     "active": true,
#     "filter": "all",        ← "all" | "meme" | "art" | "video"
#     "max_likes": 50,
#     "max_retweets": 20,
#     "joined": "2026-05-13T14:00:00"
#   }
# }

SUBSCRIBERS_FILE = "subscribers.json"

def load_subscribers() -> dict:
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_subscribers(subs: dict):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subs, f, indent=2)

subscribers: dict = load_subscribers()

# ── Seen Tweets Cache ─────────────────────────────────────────────────────────
SEEN_FILE = "seen_tweets.json"

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    trimmed = list(seen)[-5000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)

seen_tweets: set = load_seen()

# Track last scan time globally
last_scan_time: str = "Never"

# ── Search Targets ────────────────────────────────────────────────────────────
SEARCH_TARGETS = [
    ("meme contest prize",          "meme"),
    ("meme competition win",        "meme"),
    ("meme battle giveaway",        "meme"),
    ("best meme win prize",         "meme"),
    ("art contest prize",           "art"),
    ("art competition submit",      "art"),
    ("fan art contest winner",      "art"),
    ("drawing contest prize",       "art"),
    ("illustration contest win",    "art"),
    ("design contest giveaway",     "art"),
    ("video contest prize",         "video"),
    ("video competition win",       "video"),
    ("short video contest submit",  "video"),
    ("video challenge prize pool",  "video"),
    ("reel contest giveaway",       "video"),
    ("tiktok contest prize",        "video"),
]

# ── Contest Detection ─────────────────────────────────────────────────────────
CONTEST_KEYWORDS = {
    "meme": [
        "meme contest", "meme competition", "best meme", "meme battle",
        "meme challenge", "funniest meme", "submit your meme", "meme giveaway",
    ],
    "art": [
        "art contest", "art competition", "fan art contest", "drawing contest",
        "illustration contest", "design contest", "submit your art",
        "art submission", "art challenge", "creative contest",
    ],
    "video": [
        "video contest", "video competition", "short video contest",
        "video challenge", "reel contest", "tiktok contest",
        "submit your video", "video submission", "film contest",
        "clip contest", "content creator contest",
    ],
}

COMMON_KEYWORDS = [
    "enter to win", "prize pool", "winner announced", "voting open",
    "submit entries", "contest open", "contest ends", "deadline to enter",
]

PRIZE_PATTERNS = [
    r"\$[\d,]+",
    r"\d+\.?\d*\s*(eth|sol|btc|usdt|bnb|usdc)",
    r"prize[s]?\s*[:=\-]",
    r"winner[s]?\s*(get|receive|will|takes?)",
    r"(like|retweet|follow)\s*(and|&|to)\s*(enter|win|participate)",
    r"submission[s]?\s*(open|close|due|deadline)",
    r"(ends?|closes?)\s*(in|on)\s*\d+",
    r"tag\s*(a\s*friend|someone)",
    r"(reply|comment|dm)\s*(to\s*)?(enter|join|participate|submit)",
]

EXCLUDE_KEYWORDS = ["sponsored", "#ad", "paid partnership", "advertisement"]

def score_tweet(text: str, contest_type: str) -> tuple:
    lower = text.lower()
    score = 0
    reasons = []

    for exc in EXCLUDE_KEYWORDS:
        if exc in lower:
            return -1, [f"excluded: {exc}"]

    # Type-specific keywords
    for kw in CONTEST_KEYWORDS.get(contest_type, []):
        if kw in lower:
            score += 1
            reasons.append(f"keyword: {kw}")
            if len(reasons) >= 2:
                break

    # Common keywords
    for kw in COMMON_KEYWORDS:
        if kw in lower:
            score += 1
            reasons.append(f"keyword: {kw}")
            if score >= 4:
                break

    # Prize / action patterns
    for pattern in PRIZE_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            score += 2
            reasons.append(f"signal: {m.group()}")
            if score >= 8:
                break

    return score, reasons

# ── Scraper ───────────────────────────────────────────────────────────────────
def scrape_tweets(query: str, count: int = 30) -> list:
    try:
        scraper = Nitter(log_level=0, skip_instance_check=False)
        results = scraper.get_tweets(query, mode="term", number=count)
        return results.get("tweets", [])
    except Exception as e:
        log.warning(f"Scrape error '{query}': {e}")
        return []

# ── Format Alert Message ──────────────────────────────────────────────────────
def format_alert(tweet: dict, reasons: list, contest_type: str) -> str:
    likes     = tweet.get("stats", {}).get("likes", 0)
    retweets  = tweet.get("stats", {}).get("retweets", 0)
    username  = tweet.get("user", {}).get("username", "unknown")
    text      = tweet.get("text", "")[:280]
    link      = tweet.get("link", "")
    timestamp = datetime.now().strftime("%d %b %Y · %H:%M")

    emoji_map = {"meme": "🐸", "art": "🎨", "video": "🎬"}
    emoji = emoji_map.get(contest_type, "🏆")
    label = contest_type.upper()
    reasons_str = "\n".join(f"  • {r}" for r in reasons[:4])

    return (
        f"{emoji} *{label} CONTEST — LOW ENGAGEMENT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 @{username}\n"
        f"❤️ {likes} likes   🔁 {retweets} retweets\n\n"
        f"📝 _{text}_\n\n"
        f"🔍 *Detected via:*\n{reasons_str}\n\n"
        f"🔗 [Open Tweet]({link})\n"
        f"⏰ {timestamp}"
    )

# ── Send to Eligible Subscribers ─────────────────────────────────────────────
async def broadcast_alert(app, tweet: dict, reasons: list, contest_type: str, likes: int, retweets: int):
    message = format_alert(tweet, reasons, contest_type)
    sent_count = 0

    for chat_id, prefs in list(subscribers.items()):
        if not prefs.get("active", False):
            continue

        # Filter check
        user_filter = prefs.get("filter", "all")
        if user_filter != "all" and user_filter != contest_type:
            continue

        # Per-user engagement threshold
        max_likes    = prefs.get("max_likes", 50)
        max_retweets = prefs.get("max_retweets", 20)
        if likes > max_likes or retweets > max_retweets:
            continue

        try:
            await app.bot.send_message(
                chat_id=int(chat_id),
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )
            sent_count += 1
        except Exception as e:
            log.warning(f"Failed to send to {chat_id}: {e}")

    if sent_count:
        log.info(f"  📤 Alert sent to {sent_count} subscriber(s)")

# ── Core Scan Logic ───────────────────────────────────────────────────────────
async def do_scan(app):
    global last_scan_time
    log.info(f"\n{'─'*50}")
    log.info(f"⏰ Scan at {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"{'─'*50}")

    found = 0

    for query, contest_type in SEARCH_TARGETS:
        log.info(f"🔍 [{contest_type.upper()}] \"{query}\"")
        tweets = scrape_tweets(query, count=30)

        if not tweets:
            log.info("   No results.")
            continue

        for tweet in tweets:
            tweet_id = tweet.get("link", "")
            if not tweet_id or tweet_id in seen_tweets:
                continue

            seen_tweets.add(tweet_id)

            text     = tweet.get("text", "")
            likes    = tweet.get("stats", {}).get("likes", 0)
            retweets = tweet.get("stats", {}).get("retweets", 0)
            username = tweet.get("user", {}).get("username", "?")

            score, reasons = score_tweet(text, contest_type)
            if score < 3:
                continue

            log.info(f"   🎯 @{username} | score={score} | {likes}❤ {retweets}🔁")
            found += 1

            await broadcast_alert(app, tweet, reasons, contest_type, likes, retweets)
            await asyncio.sleep(1)

        await asyncio.sleep(3)

    last_scan_time = datetime.now().strftime("%d %b %Y · %H:%M")
    save_seen(seen_tweets)
    log.info(f"✅ Scan done. {found} contest(s) found.\n")
    return found

# ── Telegram Command Handlers ─────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id in subscribers and subscribers[chat_id].get("active"):
        await update.message.reply_text(
            "✅ You're already subscribed\\! Use /status to see your settings\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    subscribers[chat_id] = {
        "active": True,
        "filter": "all",
        "max_likes": 50,
        "max_retweets": 20,
        "joined": datetime.now().isoformat(),
    }
    save_subscribers(subscribers)

    await update.message.reply_text(
        "🎯 *Contest Hunter activated\\!*\n\n"
        "You'll get alerts when low\\-engagement meme, art, or video contests are spotted on X\\.\n\n"
        "📌 *Default settings:*\n"
        "  • Filter: All contest types\n"
        "  • Max likes: 50\n"
        "  • Max retweets: 20\n\n"
        "Use /help to see all commands\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    log.info(f"New subscriber: {chat_id}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("You're not subscribed. Send /start to activate alerts.")
        return

    subscribers[chat_id]["active"] = False
    save_subscribers(subscribers)
    await update.message.reply_text(
        "⏸ *Alerts paused.*\n\nSend /start anytime to reactivate\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    prefs = subscribers.get(chat_id)

    if not prefs:
        await update.message.reply_text("You're not subscribed. Send /start to activate.")
        return

    status_icon = "✅ Active" if prefs.get("active") else "⏸ Paused"
    f = prefs.get("filter", "all").upper()
    ml = prefs.get("max_likes", 50)
    mr = prefs.get("max_retweets", 20)
    joined = prefs.get("joined", "unknown")[:10]

    active_count = sum(1 for s in subscribers.values() if s.get("active"))

    await update.message.reply_text(
        f"📊 *Your Status*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Alerts: {status_icon}\n"
        f"Filter: {f}\n"
        f"Max likes: {ml}\n"
        f"Max retweets: {mr}\n"
        f"Subscribed since: {joined}\n\n"
        f"🕐 Last scan: {last_scan_time}\n"
        f"👥 Total active subscribers: {active_count}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_setfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in subscribers:
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    args = context.args
    valid = ["all", "meme", "art", "video"]

    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            "Usage: `/setfilter <type>`\n\n"
            "Options:\n"
            "  `all` — all contest types\n"
            "  `meme` — meme contests only\n"
            "  `art` — art contests only\n"
            "  `video` — video contests only",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chosen = args[0].lower()
    subscribers[chat_id]["filter"] = chosen
    save_subscribers(subscribers)

    emoji_map = {"all": "🏆", "meme": "🐸", "art": "🎨", "video": "🎬"}
    await update.message.reply_text(
        f"{emoji_map[chosen]} Filter set to *{chosen.upper()}*\\. "
        f"You'll only receive {chosen} contest alerts from now on\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in subscribers:
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    args = context.args
    if len(args) != 2 or not all(a.isdigit() for a in args):
        await update.message.reply_text(
            "Usage: `/threshold <max_likes> <max_retweets>`\n\n"
            "Example: `/threshold 30 10`\n"
            "Only contests with ≤30 likes AND ≤10 retweets will alert you.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    max_likes    = int(args[0])
    max_retweets = int(args[1])

    if max_likes < 1 or max_retweets < 1:
        await update.message.reply_text("Values must be at least 1.")
        return

    subscribers[chat_id]["max_likes"]    = max_likes
    subscribers[chat_id]["max_retweets"] = max_retweets
    save_subscribers(subscribers)

    await update.message.reply_text(
        f"⚙️ *Threshold updated\\!*\n\n"
        f"  ❤️ Max likes: {max_likes}\n"
        f"  🔁 Max retweets: {max_retweets}\n\n"
        f"Contests above these numbers will be ignored for you\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    await update.message.reply_text("🔍 Running a manual scan now... hang tight!")
    found = await do_scan(context.application)
    await update.message.reply_text(
        f"✅ Scan complete\\. *{found}* new contest(s) found and alerted\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 *Contest Hunter — Commands*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*/start* — Subscribe to contest alerts\n"
        "*/stop* — Pause your alerts\n"
        "*/status* — Your settings + last scan time\n\n"
        "*/setfilter all* — All contest types\n"
        "*/setfilter meme* — Meme contests only 🐸\n"
        "*/setfilter art* — Art contests only 🎨\n"
        "*/setfilter video* — Video contests only 🎬\n\n"
        "*/threshold 30 10* — Set max likes / retweets\n"
        "*/scan* — Trigger an instant scan now\n"
        "*/help* — Show this message\n\n"
        "💡 _Tip: Lower your threshold to get only the freshest, least-competitive contests._",
        parse_mode=ParseMode.MARKDOWN,
    )

# ── Scheduled Scan Loop (runs in background thread) ──────────────────────────
def start_scheduler(app):
    async def scheduled_scan():
        await do_scan(app)

    def run_schedule():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def job():
            loop.run_until_complete(scheduled_scan())

        schedule.every(CHECK_INTERVAL).minutes.do(job)
        log.info(f"⏰ Scheduler started — scanning every {CHECK_INTERVAL} minutes.")

        while True:
            schedule.run_pending()
            time.sleep(30)

    thread = threading.Thread(target=run_schedule, daemon=True)
    thread.start()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set in .env — exiting.")
        return

    log.info("╔══════════════════════════════════════╗")
    log.info("║   CONTEST HUNTER BOT (INTERACTIVE)   ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"  Scan interval : every {CHECK_INTERVAL} min")
    log.info(f"  Subscribers   : {len(subscribers)} loaded\n")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("stop",       cmd_stop))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("setfilter",  cmd_setfilter))
    app.add_handler(CommandHandler("threshold",  cmd_threshold))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("help",       cmd_help))

    # Set command menu visible in Telegram UI
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",      "Subscribe to contest alerts"),
            BotCommand("stop",       "Pause your alerts"),
            BotCommand("status",     "Your settings and last scan time"),
            BotCommand("setfilter",  "Filter by type: all | meme | art | video"),
            BotCommand("threshold",  "Set max likes/retweets e.g. /threshold 30 10"),
            BotCommand("scan",       "Run an instant manual scan"),
            BotCommand("help",       "Show all commands"),
        ])
        # Start background scan scheduler
        start_scheduler(application)

    app.post_init = post_init

    log.info("🤖 Bot is running. Press Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
