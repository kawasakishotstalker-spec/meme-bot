"""
╔══════════════════════════════════════════════════════════════╗
║        CRYPTO CONTEST HUNTER — Telegram Alert Bot           ║
║   Scans X/Twitter for crypto contests → Telegram alerts     ║
║   Backend: twitterapi.io                                     ║
╚══════════════════════════════════════════════════════════════╝

CATEGORIES (all crypto):
    nft       → NFT giveaways, whitelist raffles, mint contests
    memecoin  → Memecoin meme & art competitions
    project   → Airdrop, web3, DeFi, DAO, testnet contests
    exchange  → Trading competitions, PnL contests, leaderboards
    creative  → Crypto art, video, meme & thread contests

USER COMMANDS:
    /start                → Subscribe to alerts
    /stop                 → Pause alerts
    /status               → Your settings + last scan info
    /setfilter nft        → NFT contests only
    /setfilter memecoin   → Memecoin contests only
    /setfilter project    → Airdrop / web3 project contests only
    /setfilter exchange   → Trading competitions only
    /setfilter creative   → Crypto art / video / meme / thread contests only
    /setfilter all        → All categories (default)
    /threshold 30 10      → Max likes / max retweets filter
    /scan                 → Run an instant one-time scan
    /autoscan             → Toggle perpetual scan loop on/off
    /debug [query]        → Test a query, dump raw results
    /help                 → Show all commands

RAILWAY ENV VARS:
    TELEGRAM_BOT_TOKEN=...
    TWITTERAPI_KEY=...
    CHECK_INTERVAL=20
"""

import os
import re
import json
import logging
import asyncio
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

try:
    from telegram import Update, BotCommand
    from telegram.ext import Application, CommandHandler, ContextTypes
    from telegram.constants import ParseMode
except ImportError:
    print("Run: pip install python-telegram-bot[job-queue]")
    exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TWITTERAPI_KEY = os.getenv("TWITTERAPI_KEY", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 20))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("contest_bot.log"), logging.StreamHandler()],
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
    # Keep only the newest 5000 entries both in memory and on disk
    global seen_tweets
    if len(seen) > 5000:
        trimmed = set(list(seen)[-5000:])
        seen_tweets = trimmed
        seen = trimmed
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

seen_tweets: set = load_seen()
last_scan_time: str = "Never"
autoscan_tasks: dict = {}

# ── Categories ────────────────────────────────────────────────────────────────
CATEGORIES = ["nft", "memecoin", "project", "exchange", "creative"]
CATEGORY_EMOJI = {
    "nft":      "🖼",
    "memecoin": "🐸",
    "project":  "🚀",
    "exchange": "📈",
    "creative": "🎨",
}

# ── Search Targets (crypto-only, all categories) ──────────────────────────────
SEARCH_TARGETS = [
    # ── NFT ──────────────────────────────────────────────────────────────────
    ("NFT contest prize crypto",                    "nft"),
    ("NFT giveaway whitelist raffle win",           "nft"),
    ("NFT art competition prize pool",              "nft"),
    ("NFT meme contest submit winner",              "nft"),
    ("NFT mint free contest crypto",                "nft"),
    ("NFT community giveaway allowlist",            "nft"),
    ("NFT collection contest prize",                "nft"),

    # ── Memecoin ─────────────────────────────────────────────────────────────
    ("memecoin contest prize win",                  "memecoin"),
    ("meme coin meme competition giveaway",         "memecoin"),
    ("crypto memecoin art contest prize",           "memecoin"),
    ("solana memecoin meme battle win",             "memecoin"),
    ("best memecoin meme prize pool",               "memecoin"),
    ("memecoin community contest winner",           "memecoin"),
    ("pepe shib doge meme contest prize",           "memecoin"),

    # ── Project / Airdrop / Web3 ─────────────────────────────────────────────
    ("crypto airdrop contest prize submit",         "project"),
    ("web3 project contest giveaway win",           "project"),
    ("DeFi protocol contest prize pool",            "project"),
    ("new crypto token launch contest",             "project"),
    ("testnet contest prize crypto",                "project"),
    ("DAO community contest prize",                 "project"),
    ("blockchain project competition prize",        "project"),
    ("layer2 crypto contest giveaway",              "project"),
    ("altcoin project contest prize win",           "project"),

    # ── Exchange / Trading Competition ───────────────────────────────────────
    ("crypto trading contest prize pool",           "exchange"),
    ("crypto exchange trading competition win",     "exchange"),
    ("DEX trading challenge prize",                 "exchange"),
    ("futures trading contest leaderboard prize",   "exchange"),
    ("PnL contest crypto winner",                   "exchange"),
    ("spot trading competition giveaway crypto",    "exchange"),
    ("copy trading contest prize crypto",           "exchange"),
    ("crypto volume trading contest prize",         "exchange"),

    # ── Creative: crypto art / video / meme / thread ─────────────────────────
    ("crypto art contest prize",                    "creative"),
    ("crypto fan art competition win",              "creative"),
    ("web3 NFT art contest prize",                  "creative"),
    ("crypto video contest prize pool",             "creative"),
    ("crypto reel short video contest win",         "creative"),
    ("web3 video competition prize",                "creative"),
    ("crypto meme battle prize",                    "creative"),
    ("best crypto meme competition win",            "creative"),
    ("crypto thread contest prize win",             "creative"),
    ("best crypto twitter thread competition",      "creative"),
    ("crypto content creator contest prize",        "creative"),
    ("web3 community creative contest prize",       "creative"),
]

# ── Detection Keywords (crypto-only) ─────────────────────────────────────────
CONTEST_KEYWORDS = {
    "nft": [
        "nft giveaway", "nft contest", "nft competition", "nft whitelist",
        "nft mint", "nft drop", "free nft", "nft winner", "nft art contest",
        "nft meme contest", "allowlist giveaway", "nft raffle", "nft prize",
        "nft collection contest", "nft community giveaway", "wl giveaway",
        "allowlist contest", "nft holder contest",
    ],
    "memecoin": [
        "memecoin contest", "meme coin giveaway", "memecoin meme contest",
        "memecoin competition", "best memecoin meme", "memecoin community contest",
        "memecoin meme battle", "funniest memecoin meme", "memecoin art contest",
        "solana meme contest", "memecoin prize", "pepe contest",
        "doge meme contest", "shib contest", "wif contest", "bonk contest",
    ],
    "project": [
        "airdrop contest", "crypto token giveaway", "defi contest",
        "web3 contest", "crypto project giveaway", "new token contest",
        "altcoin contest", "crypto launch contest", "testnet contest",
        "mainnet giveaway", "protocol giveaway", "dao contest",
        "blockchain contest", "layer2 contest", "crypto hackathon prize",
        "web3 community contest", "crypto ambassador contest",
    ],
    "exchange": [
        "trading contest", "trading competition", "trading challenge",
        "crypto exchange contest", "dex competition", "futures contest",
        "spot trading contest", "trading prize pool", "pnl contest",
        "trading leaderboard prize", "volume contest", "copy trading contest",
        "crypto trading giveaway", "exchange leaderboard prize",
    ],
    "creative": [
        "crypto art contest", "crypto fan art contest", "web3 art contest",
        "nft art competition", "blockchain art contest", "crypto illustration contest",
        "crypto video contest", "crypto reel contest", "web3 video contest",
        "crypto short film contest", "crypto tiktok contest",
        "crypto meme battle", "crypto meme challenge", "web3 meme contest",
        "defi meme contest", "crypto meme competition",
        "crypto thread contest", "best crypto thread", "web3 thread contest",
        "crypto twitter thread prize", "crypto writing contest",
        "crypto content contest", "crypto creator contest",
    ],
}

COMMON_CRYPTO_KEYWORDS = [
    "enter to win", "prize pool", "winner announced", "winners selected",
    "submit your entry", "contest open", "contest ends", "deadline to enter",
    "voting open", "drop your wallet", "comment your wallet address",
    "retweet to enter", "follow and retweet", "tag a friend to enter",
    "best submission wins", "community vote", "like rt follow to win",
    "quote tweet to enter", "qrt to enter", "submit below",
]

CRYPTO_PRIZE_PATTERNS = [
    r"\$[\d,]+",
    r"\d+\.?\d*\s*(eth|sol|btc|usdt|usdc|bnb|matic|avax|arb|op|sui|apt|ton|"
    r"pepe|shib|doge|wif|bonk|trump|moodeng|fartcoin|popcat|floki|bome|myro)",
    r"\d+\s*(nft|whitelist\s*spot|wl\s*spot|allowlist\s*spot)",
    r"prize[s]?\s*[:=\-]",
    r"winner[s]?\s*(get|receive|will|takes?)",
    r"(like|retweet|rt|follow)\s*(and|&|\+|to)\s*(enter|win|participate)",
    r"(qrt|quote\s*tweet)\s*(to\s*)?(enter|win|participate)",
    r"(ends?|closes?)\s*(in|on)\s*\d+",
    r"drop\s*(your|a)\s*(wallet|address)",
    r"comment\s*(your\s*)?(wallet|address|sol|eth)",
    r"(reply|comment|quote|dm|qrt)\s*(to\s*)?(enter|join|participate|submit|win)",
    r"airdrop\s*(contest|giveaway|competition)",
    r"(wl|whitelist|allowlist)\s*(giveaway|contest|raffle|winner|spots?)",
    r"(mint|minting)\s*(free|contest|giveaway)",
    r"(thread|video|art|meme|reel)\s*(contest|competition|battle|challenge)",
    r"best\s+(thread|meme|art|video|reel|content)\s+wins?",
    r"submit\s+(your\s+)?(thread|meme|art|video|reel|entry)",
    r"(pnl|trading)\s*(contest|competition|leaderboard)",
    r"highest\s+(pnl|volume|profit)\s+wins?",
]

EXCLUDE_KEYWORDS = [
    "sponsored", "#ad", "paid partnership", "advertisement",
    "presale", "ido launch", "ico launch",
    "not financial advice", "nfa 🚨", "buy now", "invest now",
]

# ── Scoring ───────────────────────────────────────────────────────────────────
def score_tweet(text: str, category: str) -> tuple:
    lower = text.lower()
    score = 0
    reasons = []

    for exc in EXCLUDE_KEYWORDS:
        if exc in lower:
            return -1, [f"excluded: {exc}"]

    for kw in CONTEST_KEYWORDS.get(category, []):
        if kw in lower:
            score += 1
            reasons.append(f"keyword: {kw}")
            if score >= 2:
                break

    for kw in COMMON_CRYPTO_KEYWORDS:
        if kw in lower:
            score += 1
            reasons.append(f"signal: {kw}")
            if score >= 4:
                break

    for pattern in CRYPTO_PRIZE_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            score += 2
            reasons.append(f"pattern: {m.group()}")
            if score >= 8:
                break

    return score, reasons

# ── twitterapi.io Scraper ─────────────────────────────────────────────────────
SCRAPE_TIMEOUT  = 15
TWITTERAPI_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"

def _normalize(raw: dict) -> dict:
    author   = raw.get("author") or {}
    metrics  = raw.get("publicMetrics") or {}
    tweet_id = raw.get("id") or ""
    username = author.get("userName") or "unknown"
    url      = raw.get("url") or (
        f"https://x.com/{username}/status/{tweet_id}" if tweet_id else ""
    )
    return {
        "link":  url,
        "text":  raw.get("text") or "",
        "user":  {"username": username},
        "stats": {
            "likes":    metrics.get("likeCount") or 0,
            "retweets": metrics.get("retweetCount") or 0,
        },
    }

def _scrape_blocking(query: str, count: int) -> list:
    if not TWITTERAPI_KEY:
        log.error("TWITTERAPI_KEY not set.")
        return []
    try:
        resp = requests.get(
            TWITTERAPI_BASE,
            headers={"X-API-Key": TWITTERAPI_KEY, "Content-Type": "application/json"},
            params={"query": query, "queryType": "Latest", "count": min(count, 100)},
            timeout=SCRAPE_TIMEOUT,
        )
        resp.raise_for_status()
        return [_normalize(t) for t in resp.json().get("tweets", []) or []]
    except requests.exceptions.Timeout:
        log.warning(f"Timeout: '{query}'")
        return []
    except requests.exceptions.HTTPError as e:
        log.warning(f"HTTP error '{query}': {e} — {resp.text[:200]}")
        return []
    except Exception as e:
        log.warning(f"Scrape error '{query}': {e}")
        return []

async def scrape_tweets(query: str, count: int = 30) -> list:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _scrape_blocking(query, count)),
            timeout=SCRAPE_TIMEOUT + 5,
        )
    except asyncio.TimeoutError:
        log.warning(f"Async timeout: '{query}'")
        return []
    except Exception as e:
        log.warning(f"Async error '{query}': {e}")
        return []


# ── Recency: build a date-bounded query (last 7 days) ────────────────────────
def build_recent_query(base_query: str, days_back: int = 7) -> str:
    """Append since: / until: operators so the API returns only recent tweets."""
    now   = datetime.now(timezone.utc)
    since = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    until = (now + timedelta(days=1)).strftime("%Y-%m-%d")   # inclusive today
    return f"{base_query} since:{since} until:{until}"


# ── Deadline filter: skip contests ending in < MIN_DAYS_REMAINING ─────────────
MIN_DAYS_REMAINING = 2   # contests must have at least this many days left

# Patterns that signal how many days/hours remain
_ENDS_IN_DAYS    = re.compile(
    r"(?:ends?|closes?|deadline|last\s+day|expires?)\s+in\s+(\d+)\s*d(?:ays?)?",
    re.IGNORECASE,
)
_ENDS_IN_HOURS   = re.compile(
    r"(?:ends?|closes?|deadline|last\s+day|expires?)\s+in\s+(\d+)\s*h(?:ours?|rs?)?",
    re.IGNORECASE,
)
_HOURS_LEFT      = re.compile(r"(\d+)\s*h(?:ours?|rs?)?\s+left", re.IGNORECASE)
_DAYS_LEFT       = re.compile(r"(\d+)\s*d(?:ays?)?\s+left",      re.IGNORECASE)
_ENDS_ON_DATE    = re.compile(
    r"(?:ends?|closes?|deadline)\s*(?:on|:)?\s*"
    r"(\d{1,2})[\s/\-](\w+)(?:[\s/\-](\d{2,4}))?",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def _days_until(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - now).total_seconds() / 86_400


def has_enough_time_remaining(text: str) -> bool:
    """
    Returns True if the contest appears to have >= MIN_DAYS_REMAINING days left,
    OR if no deadline signal is found (we can't rule it out).
    Returns False only when we can positively detect the deadline is < 2 days away.
    """
    lower = text.lower()

    # "ends in X days"
    m = _ENDS_IN_DAYS.search(lower)
    if m:
        days = int(m.group(1))
        if days < MIN_DAYS_REMAINING:
            log.debug(f"  ⏳ Skipped — only {days}d remaining.")
            return False
        return True

    # "X days left"
    m = _DAYS_LEFT.search(lower)
    if m:
        days = int(m.group(1))
        if days < MIN_DAYS_REMAINING:
            log.debug(f"  ⏳ Skipped — only {days}d left.")
            return False
        return True

    # "ends in X hours" or "X hours left" → always < 1 day → skip
    if _ENDS_IN_HOURS.search(lower) or _HOURS_LEFT.search(lower):
        log.debug("  ⏳ Skipped — ends in hours.")
        return False

    # "ends on DD MonthName [YYYY]"
    m = _ENDS_ON_DATE.search(text)
    if m:
        day_str, month_str, year_str = m.group(1), m.group(2).lower(), m.group(3)
        month_num = _MONTH_MAP.get(month_str[:3])
        if month_num:
            year = int(year_str) if year_str else datetime.now(timezone.utc).year
            try:
                deadline = datetime(year, month_num, int(day_str), tzinfo=timezone.utc)
                if _days_until(deadline) < MIN_DAYS_REMAINING:
                    log.debug(f"  ⏳ Skipped — deadline {deadline.date()} is too soon.")
                    return False
            except ValueError:
                pass   # bad date → let it through

    # No recognisable deadline found → assume it's still open
    return True

# ── Alert Formatter ───────────────────────────────────────────────────────────
def _deadline_summary(text: str) -> str:
    """Return a short human-readable deadline line if detectable, else empty string."""
    lower = text.lower()
    m = _ENDS_IN_DAYS.search(lower)
    if m:
        return f"⏳ Ends in ~{m.group(1)} day(s)"
    m = _DAYS_LEFT.search(lower)
    if m:
        return f"⏳ ~{m.group(1)} day(s) left"
    if _ENDS_IN_HOURS.search(lower) or _HOURS_LEFT.search(lower):
        return "⏳ Ending very soon (hours)"
    m = _ENDS_ON_DATE.search(text)
    if m:
        day_str, month_str = m.group(1), m.group(2)
        return f"⏳ Deadline: {day_str} {month_str.title()}"
    return ""


def _escape_md(text: str) -> str:
    """Escape characters that break Telegram MarkdownV1 in plain text fields."""
    return re.sub(r"([*_`\[\]])", r"\\\1", text)


def format_alert(tweet: dict, reasons: list, category: str) -> str:
    likes        = tweet["stats"]["likes"]
    retweets     = tweet["stats"]["retweets"]
    username     = _escape_md(tweet["user"]["username"])
    link         = tweet["link"]
    safe_text    = re.sub(r"[*_`\[\]]", "", tweet["text"][:280])
    timestamp    = datetime.now().strftime("%d %b %Y · %H:%M")
    emoji        = CATEGORY_EMOJI.get(category, "🏆")
    reasons_str  = "\n".join(f"  • {r}" for r in reasons[:4])
    deadline_line = _deadline_summary(tweet["text"])

    base = (
        f"{emoji} *CRYPTO {category.upper()} CONTEST*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 @{username}\n"
        f"❤️ {likes} likes   🔁 {retweets} retweets\n\n"
        f"📝 _{safe_text}_\n\n"
        f"🔍 *Why detected:*\n{reasons_str}\n\n"
    )
    if deadline_line:
        base += f"{deadline_line}\n\n"
    base += f"🔗 [Open on X]({link})\n⏰ Found: {timestamp}"
    return base

# ── Broadcast ─────────────────────────────────────────────────────────────────
async def broadcast_alert(app, tweet: dict, reasons: list, category: str):
    likes    = tweet["stats"]["likes"]
    retweets = tweet["stats"]["retweets"]
    message  = format_alert(tweet, reasons, category)
    sent     = 0

    for chat_id, prefs in list(subscribers.items()):
        if not prefs.get("active"):
            continue
        user_filter = prefs.get("filter", "all")
        if user_filter != "all" and user_filter != category:
            continue
        if likes > prefs.get("max_likes", 50) or retweets > prefs.get("max_retweets", 20):
            continue
        try:
            await app.bot.send_message(
                chat_id=int(chat_id), text=message,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False,
            )
            sent += 1
        except Exception as e:
            log.warning(f"Send failed {chat_id}: {e}")

    if sent:
        log.info(f"  📤 Sent to {sent} subscriber(s)")

# ── Scan ──────────────────────────────────────────────────────────────────────
async def do_scan(app, progress_chat_id: str = None) -> int:
    global last_scan_time
    log.info(f"\n{'─'*50}\n⏰ Scan at {datetime.now().strftime('%H:%M:%S')}\n{'─'*50}")
    found = 0

    async def notify(text: str):
        """Single Telegram message to the requesting user only — no per-query spam."""
        if not progress_chat_id:
            return
        try:
            await app.bot.send_message(
                chat_id=int(progress_chat_id), text=text,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            )
        except Exception:
            pass

    # One clean status line at scan start (only fires when user runs /scan)
    await notify(
        f"🔍 *Scan started* — checking {len(SEARCH_TARGETS)} queries across "
        f"{len(CATEGORIES)} categories...\n_Results will appear below as found._"
    )

    for idx, (query, category) in enumerate(SEARCH_TARGETS, 1):
        recent_query = build_recent_query(query)   # last 7 days only
        log.info(f"🔍 [{idx}/{len(SEARCH_TARGETS)}] [{category.upper()}] {recent_query}")

        tweets = await scrape_tweets(recent_query, count=30)
        if not tweets:
            await asyncio.sleep(1)
            continue

        for tweet in tweets:
            link = tweet.get("link", "")
            if not link or link in seen_tweets:
                continue
            seen_tweets.add(link)

            # ── Skip contests ending in < 2 days ─────────────────────────
            if not has_enough_time_remaining(tweet["text"]):
                continue

            score, reasons = score_tweet(tweet["text"], category)
            score += 1
            reasons.insert(0, f"crypto search: {category}")

            if score < 2:
                continue

            u = tweet["user"]["username"]
            l = tweet["stats"]["likes"]
            r = tweet["stats"]["retweets"]
            log.info(f"  🎯 @{u} | score={score} | ❤️{l} 🔁{r}")
            found += 1

            # broadcast_alert already sends the full formatted alert to all subscribers
            await broadcast_alert(app, tweet, reasons, category)
            await asyncio.sleep(0.5)

        await asyncio.sleep(1)

    last_scan_time = datetime.now().strftime("%d %b %Y · %H:%M")
    save_seen(seen_tweets)
    log.info(f"✅ Done — {found} contest(s) found.\n")
    return found

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id in subscribers and subscribers[chat_id].get("active"):
        await update.message.reply_text("✅ Already subscribed! Use /status to check your settings.")
        return
    subscribers[chat_id] = {
        "active": True, "filter": "all",
        "max_likes": 50, "max_retweets": 20,
        "joined": datetime.now().isoformat(),
    }
    save_subscribers(subscribers)
    await update.message.reply_text(
        "🎯 *Crypto Contest Hunter activated!*\n\n"
        "You'll receive alerts when low-engagement crypto contests are spotted on X.\n\n"
        "📌 *Defaults:*\n"
        "  • Filter: All crypto categories\n"
        "  • Max likes: 50  |  Max retweets: 20\n\n"
        "Send /help to see all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info(f"New subscriber: {chat_id}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("You're not subscribed. Send /start to activate.")
        return
    subscribers[chat_id]["active"] = False
    save_subscribers(subscribers)
    await update.message.reply_text("⏸ Alerts paused. Send /start to reactivate.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    prefs = subscribers.get(chat_id)
    if not prefs:
        await update.message.reply_text("You're not subscribed. Send /start to activate.")
        return
    active_count = sum(1 for s in subscribers.values() if s.get("active"))
    await update.message.reply_text(
        f"📊 *Your Status*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Alerts: {'✅ Active' if prefs.get('active') else '⏸ Paused'}\n"
        f"Filter: {prefs.get('filter', 'all').upper()}\n"
        f"Max likes: {prefs.get('max_likes', 50)}\n"
        f"Max retweets: {prefs.get('max_retweets', 20)}\n"
        f"Since: {prefs.get('joined', '')[:10]}\n\n"
        f"🕐 Last scan: {last_scan_time}\n"
        f"👥 Active subscribers: {active_count}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_setfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers:
        await update.message.reply_text("Send /start first.")
        return
    args  = context.args
    valid = ["all"] + CATEGORIES
    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            "Usage: `/setfilter <category>`\n\n"
            "🖼 `nft`      — NFT giveaways & contests\n"
            "🐸 `memecoin` — Memecoin meme & art contests\n"
            "🚀 `project`  — Airdrop, web3, DeFi & DAO contests\n"
            "📈 `exchange` — Trading competitions & PnL contests\n"
            "🎨 `creative` — Crypto art, video, meme & thread contests\n"
            "🏆 `all`      — All categories",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    chosen = args[0].lower()
    subscribers[chat_id]["filter"] = chosen
    save_subscribers(subscribers)
    emoji = CATEGORY_EMOJI.get(chosen, "🏆")
    await update.message.reply_text(
        f"{emoji} Filter set to *{chosen.upper()}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers:
        await update.message.reply_text("Send /start first.")
        return
    args = context.args
    if len(args) != 2 or not all(a.isdigit() for a in args):
        await update.message.reply_text(
            "Usage: `/threshold <max_likes> <max_retweets>`\nExample: `/threshold 30 10`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ml, mr = int(args[0]), int(args[1])
    if ml < 1 or mr < 1:
        await update.message.reply_text("Values must be at least 1.")
        return
    subscribers[chat_id]["max_likes"]    = ml
    subscribers[chat_id]["max_retweets"] = mr
    save_subscribers(subscribers)
    await update.message.reply_text(
        f"⚙️ *Threshold updated!*\n❤️ Max likes: {ml}  🔁 Max retweets: {mr}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("Send /start first.")
        return
    found = await do_scan(context.application, progress_chat_id=chat_id)
    await update.message.reply_text(
        f"✅ *Scan complete* — *{found}* new contest(s) found.\n"
        f"_Only contests with 2+ days remaining are shown._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in subscribers or not subscribers[chat_id].get("active"):
        await update.message.reply_text("Send /start first.")
        return

    if chat_id in autoscan_tasks and not autoscan_tasks[chat_id].done():
        autoscan_tasks[chat_id].cancel()
        del autoscan_tasks[chat_id]
        await update.message.reply_text("⏹ Autoscan stopped. Send /autoscan to restart.")
        return

    await update.message.reply_text(
        "♾ *Autoscan started!*\n\n"
        "Continuously scanning X for crypto contests.\n"
        "Send /autoscan again to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )

    async def loop():
        while True:
            try:
                found = await do_scan(context.application)
                await asyncio.sleep(120 if found else 300)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Autoscan error: {e}")
                await asyncio.sleep(300)

    autoscan_tasks[chat_id] = asyncio.create_task(loop())
    log.info(f"♾ Autoscan started for {chat_id}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 *Crypto Contest Hunter — Commands*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*/start* — Subscribe to crypto contest alerts\n"
        "*/stop* — Pause your alerts\n"
        "*/status* — Your settings + last scan time\n\n"
        "*Filter by crypto category:*\n"
        "*/setfilter all* — All categories 🏆\n"
        "*/setfilter nft* — NFT contests 🖼\n"
        "*/setfilter memecoin* — Memecoin contests 🐸\n"
        "*/setfilter project* — Airdrop & web3 contests 🚀\n"
        "*/setfilter exchange* — Trading competitions 📈\n"
        "*/setfilter creative* — Crypto art/video/meme/threads 🎨\n\n"
        "*/threshold 30 10* — Max likes / max retweets\n"
        "*/scan* — Instant one-time scan\n"
        "*/autoscan* — Toggle continuous scanning ♾\n"
        "*/debug [query]* — Test a query, see raw results\n"
        "*/help* — This message",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = " ".join(context.args) if context.args else "NFT giveaway contest win"
    category = "nft"
    recent_query = build_recent_query(query)
    await update.message.reply_text(
        f"🧪 *Debug:* `{query}`\nFetching 5 recent tweets (last 7 days)...",
        parse_mode=ParseMode.MARKDOWN,
    )
    tweets = await scrape_tweets(recent_query, count=5)
    if not tweets:
        await update.message.reply_text(
            "❌ *No results.*\n\n"
            "Check:\n"
            "• `TWITTERAPI_KEY` is set in Railway Variables\n"
            "• The query returns results on X\n"
            "• Railway deploy logs for HTTP errors",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(f"✅ Got *{len(tweets)}* tweet(s):", parse_mode=ParseMode.MARKDOWN)
    for i, tweet in enumerate(tweets, 1):
        text     = tweet.get("text", "") or ""
        link     = tweet.get("link", "") or ""
        likes    = tweet.get("stats", {}).get("likes", 0)
        retweets = tweet.get("stats", {}).get("retweets", 0)
        username = tweet.get("user", {}).get("username", "unknown")
        score, reasons = score_tweet(text, category)
        score += 1
        preview = re.sub(r"[*_`\[\]]", "", text)[:200]
        msg = (
            f"*[{i}/{len(tweets)}]* @{_escape_md(username)}\n"
            f"❤️ {likes}  🔁 {retweets}  Score: {score}\n"
            f"Signals: {', '.join(reasons[:3]) or 'none'}\n"
            f"Link: {link}\n\n_{preview}_"
        )
        try:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)  # noqa: E501
        except Exception as e:
            await update.message.reply_text(f"[{i}] @{username} — render error: {e}\n{link}")
        await asyncio.sleep(0.3)

# ── Scheduler ─────────────────────────────────────────────────────────────────
def start_scheduler(app):
    async def _run_scan(ctx):
        await do_scan(app)

    app.job_queue.run_once(_run_scan, when=30)
    app.job_queue.run_repeating(
        _run_scan,
        interval=CHECK_INTERVAL * 60,
        first=CHECK_INTERVAL * 60,
    )
    log.info(f"⏰ First scan in 30s, then every {CHECK_INTERVAL} min.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set — exiting.")
        return
    if not TWITTERAPI_KEY:
        log.error("TWITTERAPI_KEY not set — exiting.")
        return

    log.info("╔══════════════════════════════════════╗")
    log.info("║   CRYPTO CONTEST HUNTER BOT          ║")
    log.info("║   Backend : twitterapi.io             ║")
    log.info(f"║   Interval: every {CHECK_INTERVAL} min              ║")
    log.info("╚══════════════════════════════════════╝")

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",     "Subscribe to crypto contest alerts"),
            BotCommand("stop",      "Pause your alerts"),
            BotCommand("status",    "Your settings and last scan time"),
            BotCommand("setfilter", "all | nft | memecoin | project | exchange | creative"),
            BotCommand("threshold", "Max likes/retweets — e.g. /threshold 30 10"),
            BotCommand("scan",      "Run an instant scan now"),
            BotCommand("autoscan",  "Toggle continuous scanning on/off"),
            BotCommand("help",      "Show all commands"),
        ])
        start_scheduler(application)

    async def post_shutdown(application):
        for task in list(autoscan_tasks.values()):
            if not task.done():
                task.cancel()
        log.info("🛑 All autoscan tasks cancelled on shutdown.")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    for cmd, fn in [
        ("start",     cmd_start),
        ("stop",      cmd_stop),
        ("status",    cmd_status),
        ("setfilter", cmd_setfilter),
        ("threshold", cmd_threshold),
        ("scan",      cmd_scan),
        ("autoscan",  cmd_autoscan),
        ("help",      cmd_help),
        ("debug",     cmd_debug),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    log.info("🤖 Bot running.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
