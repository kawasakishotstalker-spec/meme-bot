"""
╔══════════════════════════════════════════════════════════════╗
║        CONTEST HUNTER BOT — Interactive Telegram Version     ║
║   Scrapes Twitter/X → Alerts subscribers via Telegram        ║
║   Backend: twitterapi.io  (replaces ntscraper/Nitter)        ║
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
    /scan               → Trigger an instant one-time scan
    /autoscan           → Toggle perpetual scan loop on/off
    /help               → Show all commands

SETUP:
    pip install requests python-telegram-bot python-dotenv

.env file:
    TELEGRAM_BOT_TOKEN=your_bot_token
    TWITTERAPI_KEY=your_twitterapi_io_key
    CHECK_INTERVAL=20
"""

import os
import re
import json
import logging
import asyncio
import requests
from datetime import datetime
from dotenv import load_dotenv

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
TWITTERAPI_KEY  = os.getenv("TWITTERAPI_KEY", "")
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

# Autoscan state — tracks the running loop task per chat
autoscan_tasks: dict = {}  # chat_id -> asyncio.Task

# ── Search Targets ────────────────────────────────────────────────────────────
# Merged OR queries: 4 API calls instead of 16 — 75% fewer credits used.
# twitterapi.io supports Twitter's native OR operator natively.
SEARCH_TARGETS = [
    (
        "(meme contest OR meme competition OR meme battle OR best meme) (prize OR win OR giveaway)",
        "meme",
    ),
    (
        "(art contest OR art competition OR fan art contest OR drawing contest OR illustration contest OR design contest) (prize OR win OR submit OR giveaway)",
        "art",
    ),
    (
        "(video contest OR video competition OR reel contest OR tiktok contest OR short video contest) (prize OR win OR submit OR giveaway)",
        "video",
    ),
    (
        "(enter to win OR prize pool OR contest ends OR submit your entry OR winner announced) (meme OR art OR video OR creative OR design)",
        "art",
    ),
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

# ── twitterapi.io Scraper ─────────────────────────────────────────────────────
SCRAPE_TIMEOUT = 15  # seconds per HTTP request

TWITTERAPI_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"

def _normalize_tweet(raw: dict) -> dict:
    """
    Convert a twitterapi.io tweet object into the same shape the rest of the
    bot expects so nothing else in the code needs to change.

    twitterapi.io response fields (relevant subset):
        id, text, author.userName, publicMetrics.likeCount,
        publicMetrics.retweetCount, url
    """
    author   = raw.get("author", {})
    metrics  = raw.get("publicMetrics", {})
    tweet_id = raw.get("id", "")
    username = author.get("userName", "unknown")
    url      = raw.get("url", "") or f"https://x.com/{username}/status/{tweet_id}"

    return {
        "link": url,
        "text": raw.get("text", ""),
        "user": {"username": username},
        "stats": {
            "likes":    metrics.get("likeCount", 0),
            "retweets": metrics.get("retweetCount", 0),
        },
    }

def _scrape_blocking(query: str, count: int) -> list:
    """
    Calls the twitterapi.io advanced search endpoint synchronously.
    Runs inside a thread so it doesn't block the event loop.
    """
    if not TWITTERAPI_KEY:
        log.error("TWITTERAPI_KEY not set — cannot scrape.")
        return []

    headers = {
        "X-API-Key": TWITTERAPI_KEY,
        "Content-Type": "application/json",
    }

    params = {
        "query":    query,
        "queryType": "Latest",   # "Latest" | "Top"
        "count":    min(count, 100),
    }

    try:
        resp = requests.get(
            TWITTERAPI_BASE,
            headers=headers,
            params=params,
            timeout=SCRAPE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_tweets = data.get("tweets", []) or []
        return [_normalize_tweet(t) for t in raw_tweets]
    except requests.exceptions.Timeout:
        log.warning(f"twitterapi.io timeout for query: '{query}'")
        return []
    except requests.exceptions.HTTPError as e:
        log.warning(f"twitterapi.io HTTP error '{query}': {e} — {resp.text[:200]}")
        return []
    except Exception as e:
        log.warning(f"twitterapi.io error '{query}': {e}")
        return []

async def scrape_tweets(query: str, count: int = 30) -> list:
    loop = asyncio.get_event_loop()
    try:
        tweets = await asyncio.wait_for(
            loop.run_in_executor(None, _scrape_blocking, query, count),
            timeout=SCRAPE_TIMEOUT + 5,
        )
        return tweets
    except asyncio.TimeoutError:
        log.warning(f"Async timeout for query: '{query}' — skipping")
        return []
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
async def scan_one_query(app, query: str, contest_type: str) -> int:
    """Run a single query, score results, broadcast matches. Returns count found."""
    tweets = await scrape_tweets(query, count=50)
    found = 0

    for tweet in tweets:
        tweet_id = tweet.get("link", "")
        if not tweet_id or tweet_id in seen_tweets:
            continue

        seen_tweets.add(tweet_id)

        text     = tweet.get("text", "")
        likes    = tweet.get("stats", {}).get("likes", 0)
        retweets = tweet.get("stats", {}).get("retweets", 0)
        username = tweet.get("user", {}).get("username", "?")

        # Auto-detect contest type from tweet text for cross-type query
        detected_type = contest_type
        lower = text.lower()
        if contest_type == "art" and ("meme" in lower or "meme contest" in lower):
            detected_type = "meme"
        elif contest_type == "art" and ("video" in lower or "reel" in lower or "tiktok" in lower):
            detected_type = "video"

        score, reasons = score_tweet(text, detected_type)
        score += 1  # bonus: came from a targeted search
        reasons.insert(0, f"matched search: {detected_type}")

        if score < 2:
            continue

        log.info(f"   🎯 [{detected_type.upper()}] @{username} | score={score} | {likes}❤ {retweets}🔁")
        found += 1
        await broadcast_alert(app, tweet, reasons, detected_type, likes, retweets)

    return found


async def do_scan(app, progress_chat_id: str = None) -> int:
    """
    Run all 4 search queries concurrently. Returns total contests found.
    """
    global last_scan_time
    log.info(f"\n{'─'*50}")
    log.info(f"⏰ Scan at {datetime.now().strftime('%H:%M:%S')} — {len(SEARCH_TARGETS)} queries (concurrent)")
    log.info(f"{'─'*50}")

    async def notify(text: str):
        if not progress_chat_id:
            return
        try:
            await app.bot.send_message(
                chat_id=int(progress_chat_id),
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning(f"Notify failed: {e}")

    # Run all queries simultaneously — total time = slowest query, not sum
    tasks = [scan_one_query(app, query, ct) for query, ct in SEARCH_TARGETS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    found = sum(r for r in results if isinstance(r, int))

    last_scan_time = datetime.now().strftime("%d %b %Y · %H:%M")
    save_seen(seen_tweets)
    log.info(f"✅ Scan done. {found} contest(s) found.\n")

    if progress_chat_id:
        msg = (
            f"✅ *Scan complete* — *{found}* new contest(s) found and alerted\\."
            if found else
            "✅ *Scan complete* — no new contests this round\\."
        )
        await notify(msg)

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

    await update.message.reply_text(
        "🔍 Scanning X now\\.\\.\\. any contests found will arrive immediately\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await do_scan(context.application, progress_chat_id=chat_id)


async def cmd_autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    # If already running — stop it
    if chat_id in autoscan_tasks and not autoscan_tasks[chat_id].done():
        autoscan_tasks[chat_id].cancel()
        del autoscan_tasks[chat_id]
        await update.message.reply_text(
            "⏹ *Autoscan stopped.*\n\nSend /autoscan again to restart\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Start perpetual scan loop
    await update.message.reply_text(
        "♾ *Autoscan started\\!*\n\n"
        "I'll scan X continuously and alert you the moment a new contest appears\\.\n"
        "Send /autoscan again to stop\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async def perpetual_loop():
        app = context.application
        COOLDOWN_ACTIVE = 120   # 2 min when contests found — keep scanning fast
        COOLDOWN_IDLE   = 300   # 5 min when idle — saves credits
        while True:
            try:
                found = await do_scan(app)
                cooldown = COOLDOWN_ACTIVE if found else COOLDOWN_IDLE
                log.info(f"\u267e Autoscan sleeping {cooldown}s (found={found})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Autoscan loop error: {e}")
                cooldown = COOLDOWN_IDLE
            await asyncio.sleep(cooldown)

    task = asyncio.create_task(perpetual_loop())
    autoscan_tasks[chat_id] = task
    log.info(f"♾ Autoscan started for {chat_id}")


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
        "*/scan* — Run an instant one-time scan now\n"
        "*/autoscan* — Toggle perpetual scan loop on/off ♾\n"
        "*/debug* — Test one query and dump raw results\n"
        "*/help* — Show this message\n\n"
        "💡 _Tip: Use /autoscan to never miss a contest — it scans X non-stop._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Runs a single test query and dumps everything twitterapi.io returns — raw
    tweet text, stats, link, and score — so you can see exactly why tweets are
    passing or failing.

    Usage:
        /debug               → uses default query "meme contest prize"
        /debug art contest   → uses custom query
    """
    query = " ".join(context.args) if context.args else "meme contest prize"
    contest_type = "meme"

    await update.message.reply_text(
        f"🧪 *Debug scrape:* `{query}`\nFetching up to 5 tweets from twitterapi.io...",
        parse_mode=ParseMode.MARKDOWN,
    )

    tweets = await scrape_tweets(query, count=5)

    if not tweets:
        await update.message.reply_text(
            "❌ *No tweets returned.*\n\n"
            "Possible causes:\n"
            "• TWITTERAPI_KEY not set or invalid\n"
            "• Query returned zero results on X\n"
            "• Network error (check contest_bot.log for details)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"✅ Got *{len(tweets)}* tweet(s). Showing details below:",
        parse_mode=ParseMode.MARKDOWN,
    )

    for i, tweet in enumerate(tweets, start=1):
        raw_text = tweet.get("text", "(no text)") or "(no text)"
        link     = tweet.get("link", "(no link)") or "(no link)"
        likes    = tweet.get("stats", {}).get("likes", "?")
        retweets = tweet.get("stats", {}).get("retweets", "?")
        username = tweet.get("user", {}).get("username", "?")
        score, reasons = score_tweet(raw_text, contest_type)
        score += 1  # query-match bonus
        reasons_str = ", ".join(reasons[:4]) or "none"

        preview = raw_text[:200].replace("*", "").replace("_", "").replace("`", "")

        msg = (
            f"*[{i}/{len(tweets)}]* @{username}\n"
            f"❤️ {likes}  🔁 {retweets}\n"
            f"Score: {score}  |  Reasons: {reasons_str}\n"
            f"Link: {link}\n\n"
            f"_{preview}_"
        )
        try:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                            disable_web_page_preview=True)
        except Exception as e:
            await update.message.reply_text(
                f"[{i}] @{username} — could not render (parse error: {e})\nLink: {link}"
            )
        await asyncio.sleep(0.3)

# ── Scheduled Scan Loop ───────────────────────────────────────────────────────
def start_scheduler(app):
    app.job_queue.run_once(lambda ctx: asyncio.ensure_future(do_scan(app)), when=30)
    app.job_queue.run_repeating(
        lambda ctx: asyncio.ensure_future(do_scan(app)),
        interval=CHECK_INTERVAL * 60,
        first=CHECK_INTERVAL * 60,
    )
    log.info(f"⏰ Scheduler started — first scan in 30s, then every {CHECK_INTERVAL} minutes.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set in .env — exiting.")
        return

    if not TWITTERAPI_KEY:
        log.error("TWITTERAPI_KEY not set in .env — exiting.")
        return

    log.info("╔══════════════════════════════════════╗")
    log.info("║   CONTEST HUNTER BOT (INTERACTIVE)   ║")
    log.info("║   Backend: twitterapi.io              ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"  Scan interval : every {CHECK_INTERVAL} min")
    log.info(f"  Subscribers   : {len(subscribers)} loaded\n")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("stop",       cmd_stop))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("setfilter",  cmd_setfilter))
    app.add_handler(CommandHandler("threshold",  cmd_threshold))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("autoscan",   cmd_autoscan))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("debug",      cmd_debug))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",      "Subscribe to contest alerts"),
            BotCommand("stop",       "Pause your alerts"),
            BotCommand("status",     "Your settings and last scan time"),
            BotCommand("setfilter",  "Filter by type: all | meme | art | video"),
            BotCommand("threshold",  "Set max likes/retweets e.g. /threshold 30 10"),
            BotCommand("scan",       "Run an instant one-time scan"),
            BotCommand("autoscan",   "Toggle perpetual scan loop on/off"),
            BotCommand("help",       "Show all commands"),
        ])
        start_scheduler(application)

    app.post_init = post_init

    log.info("🤖 Bot is running. Press Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
