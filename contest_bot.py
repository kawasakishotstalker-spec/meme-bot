"""
╔══════════════════════════════════════════════════════════════╗
║        CONTEST HUNTER BOT — Interactive Telegram Version     ║
║   Scrapes Twitter/X → Alerts subscribers via Telegram        ║
║   Backend: twitterapi.io  (REST API, no login required)      ║
╚══════════════════════════════════════════════════════════════╝

USER COMMANDS:
    /start              → Subscribe to contest alerts
    /stop               → Pause your alerts
    /status             → Check subscription + last scan info
    /setfilter nft      → Only get NFT contest alerts
    /setfilter memecoin → Only get meme coin contest alerts
    /setfilter project  → Only get new project/airdrop alerts
    /setfilter exchange → Only get trading competition alerts
    /setfilter all      → Get all contest types (default)
    /threshold 30 10    → Set max likes / max retweets
    /scan               → Trigger an instant one-time scan
    /autoscan           → Toggle perpetual scan loop on/off
    /debug [query]      → Test a query and dump raw results
    /help               → Show all commands

SETUP:
    pip install -r requirements.txt

Railway environment variables:
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
    print("Run: pip install python-telegram-bot[job-queue]")
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

last_scan_time: str = "Never"
autoscan_tasks: dict = {}  # chat_id -> asyncio.Task

# ── Search Targets (Crypto-focused) ──────────────────────────────────────────
SEARCH_TARGETS = [
    # NFT contests
    ("NFT giveaway contest win",            "nft"),
    ("NFT meme contest prize",              "nft"),
    ("NFT art competition submit",          "nft"),
    ("NFT whitelist giveaway",              "nft"),
    ("NFT mint giveaway winner",            "nft"),
    # Meme coin contests
    ("memecoin meme contest prize",         "memecoin"),
    ("meme coin competition giveaway",      "memecoin"),
    ("crypto meme contest win",             "memecoin"),
    ("best crypto meme prize pool",         "memecoin"),
    ("memecoin community contest",          "memecoin"),
    # New project / token launches
    ("crypto airdrop contest submit",       "project"),
    ("new token launch giveaway",           "project"),
    ("DeFi project contest prize",          "project"),
    ("crypto project competition win",      "project"),
    ("web3 project giveaway contest",       "project"),
    ("altcoin giveaway contest",            "project"),
    # Exchange / trading competitions
    ("crypto exchange trading contest",     "exchange"),
    ("exchange trading competition prize",  "exchange"),
    ("crypto trading challenge prize pool", "exchange"),
    ("DEX trading competition win",         "exchange"),
    ("futures trading contest prize",       "exchange"),
]

# ── Contest Detection ─────────────────────────────────────────────────────────
CONTEST_KEYWORDS = {
    "nft": [
        "nft giveaway", "nft contest", "nft competition", "nft whitelist",
        "nft mint", "nft drop", "free nft", "nft winner", "nft art contest",
        "nft meme contest", "allowlist giveaway", "nft raffle",
    ],
    "memecoin": [
        "memecoin contest", "meme coin giveaway", "crypto meme contest",
        "meme competition", "best crypto meme", "memecoin community contest",
        "meme battle crypto", "funniest crypto meme", "coin meme challenge",
    ],
    "project": [
        "airdrop contest", "token giveaway", "defi contest", "web3 contest",
        "crypto project giveaway", "new token contest", "altcoin giveaway",
        "crypto launch contest", "testnet contest", "mainnet giveaway",
        "protocol giveaway", "dao contest", "crypto community contest",
    ],
    "exchange": [
        "trading contest", "trading competition", "trading challenge",
        "exchange contest", "dex competition", "futures contest",
        "spot trading contest", "trading prize pool", "pnl contest",
        "crypto trading giveaway", "leaderboard prize",
    ],
}

COMMON_KEYWORDS = [
    "enter to win", "prize pool", "winner announced", "winners selected",
    "submit entries", "contest open", "contest ends", "deadline to enter",
    "voting open", "drop your wallet", "comment your wallet",
    "retweet to enter", "follow and retweet", "tag a friend",
]

PRIZE_PATTERNS = [
    r"\$[\d,]+",
    r"\d+\.?\d*\s*(eth|sol|btc|usdt|bnb|usdc|matic|avax|arb|op|sui|apt|ton|pepe|shib|doge|wif|bonk)",
    r"\d+\s*(nft|whitelist|wl\s*spot)",
    r"prize[s]?\s*[:=\-]",
    r"winner[s]?\s*(get|receive|will|takes?)",
    r"(like|retweet|follow)\s*(and|&|to)\s*(enter|win|participate)",
    r"(ends?|closes?)\s*(in|on)\s*\d+",
    r"drop\s*(your|a)\s*wallet",
    r"(reply|comment|dm)\s*(to\s*)?(enter|join|participate|submit|win)",
    r"airdrop\s*(contest|giveaway|competition)",
    r"(wl|whitelist)\s*(giveaway|contest|raffle|winner)",
    r"(mint|minting)\s*(free|contest|giveaway)",
]

EXCLUDE_KEYWORDS = ["sponsored", "#ad", "paid partnership", "advertisement", "buy now", "invest now"]

def score_tweet(text: str, contest_type: str) -> tuple:
    lower = text.lower()
    score = 0
    reasons = []

    for exc in EXCLUDE_KEYWORDS:
        if exc in lower:
            return -1, [f"excluded: {exc}"]

    for kw in CONTEST_KEYWORDS.get(contest_type, []):
        if kw in lower:
            score += 1
            reasons.append(f"keyword: {kw}")
            if len(reasons) >= 2:
                break

    for kw in COMMON_KEYWORDS:
        if kw in lower:
            score += 1
            reasons.append(f"keyword: {kw}")
            if score >= 4:
                break

    for pattern in PRIZE_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            score += 2
            reasons.append(f"signal: {m.group()}")
            if score >= 8:
                break

    return score, reasons

# ── twitterapi.io Scraper ─────────────────────────────────────────────────────
SCRAPE_TIMEOUT   = 15
TWITTERAPI_BASE  = "https://api.twitterapi.io/twitter/tweet/advanced_search"

def _normalize_tweet(raw: dict) -> dict:
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
    if not TWITTERAPI_KEY:
        log.error("TWITTERAPI_KEY not set — cannot scrape.")
        return []
    headers = {
        "X-API-Key":    TWITTERAPI_KEY,
        "Content-Type": "application/json",
    }
    params = {
        "query":     query,
        "queryType": "Latest",
        "count":     min(count, 100),
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

    emoji_map = {"nft": "🖼", "memecoin": "🐸", "project": "🚀", "exchange": "📈"}
    emoji = emoji_map.get(contest_type, "🏆")
    label = contest_type.upper()
    reasons_str = "\n".join(f"  • {r}" for r in reasons[:4])

    # Escape characters that break Telegram Markdown
    safe_text = text.replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")

    return (
        f"{emoji} *{label} CONTEST — LOW ENGAGEMENT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 @{username}\n"
        f"❤️ {likes} likes   🔁 {retweets} retweets\n\n"
        f"📝 _{safe_text}_\n\n"
        f"🔍 *Detected via:*\n{reasons_str}\n\n"
        f"🔗 [Open Tweet]({link})\n"
        f"⏰ {timestamp}"
    )

# ── Broadcast Alert ───────────────────────────────────────────────────────────
async def broadcast_alert(app, tweet: dict, reasons: list, contest_type: str, likes: int, retweets: int):
    message = format_alert(tweet, reasons, contest_type)
    sent_count = 0

    for chat_id, prefs in list(subscribers.items()):
        if not prefs.get("active", False):
            continue

        user_filter = prefs.get("filter", "all")
        if user_filter != "all" and user_filter != contest_type:
            continue

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
async def do_scan(app, progress_chat_id: str = None) -> int:
    global last_scan_time
    log.info(f"\n{'─'*50}")
    log.info(f"⏰ Scan at {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"{'─'*50}")

    found = 0
    total = len(SEARCH_TARGETS)

    async def ping(text: str):
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
            log.warning(f"Progress ping failed: {e}")

    for idx, (query, contest_type) in enumerate(SEARCH_TARGETS, start=1):
        log.info(f"🔍 [{contest_type.upper()}] \"{query}\"")
        await ping(f"🔍 `[{idx}/{total}]` Checking *{contest_type}* — _{query}_")

        tweets = await scrape_tweets(query, count=30)

        if not tweets:
            log.info("   No results.")
            await asyncio.sleep(1)
            continue

        batch_found = 0
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
            score += 1
            reasons.insert(0, f"matched search: {query}")

            if score < 2:
                continue

            log.info(f"   🎯 @{username} | score={score} | {likes}❤ {retweets}🔁")
            found += 1
            batch_found += 1

            await ping(
                f"🎯 *Found a {contest_type} contest!*\n"
                f"👤 @{username} · ❤️ {likes} · 🔁 {retweets}\n"
                f"🔗 {tweet_id}"
            )
            await broadcast_alert(app, tweet, reasons, contest_type, likes, retweets)
            await asyncio.sleep(0.5)

        if batch_found == 0:
            log.info("   No qualifying contests in results.")

        await asyncio.sleep(1)

    last_scan_time = datetime.now().strftime("%d %b %Y · %H:%M")
    save_seen(seen_tweets)
    log.info(f"✅ Scan done. {found} contest(s) found.\n")
    return found

# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id in subscribers and subscribers[chat_id].get("active"):
        await update.message.reply_text("✅ You're already subscribed! Use /status to see your settings.")
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
        "🎯 *Contest Hunter activated!*\n\n"
        "You'll get alerts when low-engagement crypto contests are spotted on X.\n\n"
        "📌 *Default settings:*\n"
        "  • Filter: All contest types\n"
        "  • Max likes: 50\n"
        "  • Max retweets: 20\n\n"
        "Use /help to see all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info(f"New subscriber: {chat_id}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("You're not subscribed. Send /start to activate alerts.")
        return
    subscribers[chat_id]["active"] = False
    save_subscribers(subscribers)
    await update.message.reply_text("⏸ Alerts paused. Send /start anytime to reactivate.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    prefs = subscribers.get(chat_id)
    if not prefs:
        await update.message.reply_text("You're not subscribed. Send /start to activate.")
        return

    status_icon  = "✅ Active" if prefs.get("active") else "⏸ Paused"
    f            = prefs.get("filter", "all").upper()
    ml           = prefs.get("max_likes", 50)
    mr           = prefs.get("max_retweets", 20)
    joined       = prefs.get("joined", "unknown")[:10]
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

    valid = ["all", "nft", "memecoin", "project", "exchange"]
    args  = context.args

    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            "Usage: `/setfilter <type>`\n\n"
            "Options:\n"
            "  `all`      — all contest types\n"
            "  `nft`      — NFT giveaways & contests 🖼\n"
            "  `memecoin` — meme coin competitions 🐸\n"
            "  `project`  — new project & airdrop contests 🚀\n"
            "  `exchange` — trading competitions 📈",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chosen = args[0].lower()
    subscribers[chat_id]["filter"] = chosen
    save_subscribers(subscribers)

    emoji_map = {"all": "🏆", "nft": "🖼", "memecoin": "🐸", "project": "🚀", "exchange": "📈"}
    await update.message.reply_text(
        f"{emoji_map[chosen]} Filter set to *{chosen.upper()}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers:
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    args = context.args
    if len(args) != 2 or not all(a.isdigit() for a in args):
        await update.message.reply_text(
            "Usage: `/threshold <max_likes> <max_retweets>`\n\nExample: `/threshold 30 10`",
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
        f"⚙️ *Threshold updated!*\n\n❤️ Max likes: {max_likes}\n🔁 Max retweets: {max_retweets}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    await update.message.reply_text("🔍 Scanning X now... contests found will arrive immediately.")
    found = await do_scan(context.application, progress_chat_id=chat_id)
    await update.message.reply_text(f"✅ Scan complete. *{found}* new contest(s) found.", parse_mode=ParseMode.MARKDOWN)


async def cmd_autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("Send /start first to activate alerts.")
        return

    if chat_id in autoscan_tasks and not autoscan_tasks[chat_id].done():
        autoscan_tasks[chat_id].cancel()
        del autoscan_tasks[chat_id]
        await update.message.reply_text("⏹ Autoscan stopped. Send /autoscan again to restart.")
        return

    await update.message.reply_text(
        "♾ *Autoscan started!*\n\nI'll scan X continuously and alert you the moment a new contest appears.\nSend /autoscan again to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )

    async def perpetual_loop():
        app = context.application
        while True:
            try:
                found = await do_scan(app)
                cooldown = 120 if found else 300
                log.info(f"♾ Autoscan sleeping {cooldown}s")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Autoscan loop error: {e}")
                cooldown = 300
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
        "*/setfilter nft* — NFT contests only 🖼\n"
        "*/setfilter memecoin* — Meme coin contests only 🐸\n"
        "*/setfilter project* — New project/airdrop alerts 🚀\n"
        "*/setfilter exchange* — Trading competitions only 📈\n\n"
        "*/threshold 30 10* — Set max likes / retweets\n"
        "*/scan* — Run an instant one-time scan\n"
        "*/autoscan* — Toggle perpetual scan loop on/off ♾\n"
        "*/debug [query]* — Test a query and dump raw results\n"
        "*/help* — Show this message",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query        = " ".join(context.args) if context.args else "NFT giveaway contest win"
    contest_type = "nft"

    await update.message.reply_text(
        f"🧪 *Debug scrape:* `{query}`\nFetching up to 5 tweets from twitterapi.io...",
        parse_mode=ParseMode.MARKDOWN,
    )

    tweets = await scrape_tweets(query, count=5)

    if not tweets:
        await update.message.reply_text(
            "❌ *No tweets returned.*\n\n"
            "Possible causes:\n"
            "• TWITTERAPI_KEY not set or invalid in Railway Variables\n"
            "• Query returned zero results on X\n"
            "• Network error — check Railway logs for details",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(f"✅ Got *{len(tweets)}* tweet(s):", parse_mode=ParseMode.MARKDOWN)

    for i, tweet in enumerate(tweets, start=1):
        raw_text = tweet.get("text", "(no text)") or "(no text)"
        link     = tweet.get("link", "(no link)") or "(no link)"
        likes    = tweet.get("stats", {}).get("likes", "?")
        retweets = tweet.get("stats", {}).get("retweets", "?")
        username = tweet.get("user", {}).get("username", "?")
        score, reasons = score_tweet(raw_text, contest_type)
        score += 1
        reasons_str = ", ".join(reasons[:4]) or "none"
        preview = raw_text[:200].replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")

        msg = (
            f"*[{i}/{len(tweets)}]* @{username}\n"
            f"❤️ {likes}  🔁 {retweets}\n"
            f"Score: {score}  |  Reasons: {reasons_str}\n"
            f"Link: {link}\n\n"
            f"_{preview}_"
        )
        try:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        except Exception as e:
            await update.message.reply_text(f"[{i}] @{username} — render error: {e}\nLink: {link}")
        await asyncio.sleep(0.3)

# ── Scheduler ─────────────────────────────────────────────────────────────────
def start_scheduler(app):
    app.job_queue.run_once(lambda ctx: asyncio.ensure_future(do_scan(app)), when=30)
    app.job_queue.run_repeating(
        lambda ctx: asyncio.ensure_future(do_scan(app)),
        interval=CHECK_INTERVAL * 60,
        first=CHECK_INTERVAL * 60,
    )
    log.info(f"⏰ Scheduler started — first scan in 30s, then every {CHECK_INTERVAL} min.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set — exiting.")
        return
    if not TWITTERAPI_KEY:
        log.error("TWITTERAPI_KEY not set — exiting.")
        return

    log.info("╔══════════════════════════════════════╗")
    log.info("║   CONTEST HUNTER BOT — CRYPTO MODE   ║")
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
            BotCommand("setfilter",  "Filter: all | nft | memecoin | project | exchange"),
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
