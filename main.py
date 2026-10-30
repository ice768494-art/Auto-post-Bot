import os
import re
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import html
import sqlite3
from contextlib import closing
from urllib.parse import urlparse
from telegram.constants import ParseMode

# ==================== LOGGING ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
STARTED_AT = time.time()
METRICS = {"episode_posts_ok": 0, "episode_posts_failed": 0,
           "upload_posts_ok": 0, "upload_posts_failed": 0}

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "").strip()
SOURCE_CHANNEL_IDS = [
    channel.strip()
    for channel in os.getenv("SOURCE_CHANNEL_IDS", "").split(",")
    if channel.strip()
]
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing from environment variables!")
if not MAIN_CHANNEL_ID:
    raise ValueError("MAIN_CHANNEL_ID is missing from environment variables!")
if not SOURCE_CHANNEL_IDS:
    raise ValueError("SOURCE_CHANNEL_IDS is missing from environment variables!")

# ANuploadvot configuration. These are optional if only Auto-post-Bot is used.
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().lstrip("-").isdigit()
}
DATABASE_PATH = os.getenv("DATABASE_PATH", "/var/data/anime_channels.sqlite3").strip()


# ==================== ANIME CHANNEL LINKS ====================
ANIME_CHANNEL_LINKS = {
    "naruto shippuden": "https://t.me/+uKuAaVBzmSs0ZDll",
    "liar game": "https://t.me/+ki2AYsqJafs2NDk1",
    "the laid-off cheat-granting mage enjoys a second lease on life": "https://t.me/+Sij9-LGXOmFjYTA1",
    "Tomb Raider King" : "https://t.me/+1d2421FenfNlZGM9",
    "The Exiled Heavy Knight Knows How To Game The System": "https://t.me/+SppUueMG-bdiMjI1",
    "The Ramparts Of Ice" : "https://t.me/+eGLMJuQ2lTA4YTBl",
    "JoJo’s Bizarre Adventure Steel Ball Run" : "https://t.me/+4iT4nt1SFNE2Yzdl",
    "The Apothecary Diaries" : "https://t.me/+Ab5t_ExrlXkyOTNl",
    "A Wild Last Boss Appeared" : "https://t.me/+Hdz90USLCW4yZjE1",
    "Black Clover" : "https://t.me/+WTEdJxxK8OU0ODBl",
    "Blue Box" : "https://t.me/+NNlAczU_448zZTI1",
    "Psyren" : "https://t.me/+qb7j7gch9rg5NjM1",
    "Overgeared" : "https://t.me/+mzi8whxY721mZmM1",
    "Sword Art Online" : "https://t.me/+4WesuhOsbsUwNmM1",
    "Firefly Wedding" : "https://t.me/+Rse64X9j-idlYjRl",
    "As A Reincarnated Aristocrat I'll Use My Appraisal Skill To Rise The World" : "https://t.me/+k2ETQ1m_J3lhZmQ1",
    "Even Though I'm a Super Timid Noble Girl, I Accepted the Bet From My Cunning Fiancé" : "https://t.me/+iperfm4Spv0wMzI9",
    # Replace this example link with your real Demon Slayer channel link.
    "demon slayer": "https://t.me/DemonSlayerChannel",
}
DEFAULT_CHANNEL_LINK = "https://t.me/AnimeNetworkTamil"

# ==================== ANIME POSTER URLS ====================
# Each URL should resolve directly to an image Telegram can fetch.
ANIME_POSTERS = {
    "naruto shippuden": "https://imgyx.pages.dev/VQw3e",
    "liar game": "https://imgyx.pages.dev/sjYWT",
    "the laid-off cheat-granting mage enjoys a second lease on life": "https://imgyx.pages.dev/67la6",
    "Tomb Raider King":"https://imgyx.pages.dev/gKj9J",
    "The Exiled Heavy Knight Knows How To Game The System" : "https://imgyx.pages.dev/xNjX5",
    "The Ramparts Of Ice" : "https://imgyx.pages.dev/cckx3",
    "JoJo’s Bizarre Adventure Steel Ball Run" : "https://imgyx.pages.dev/NDUnT",
    "The Apothecary Diaries" : "https://imgyx.pages.dev/JaNj9",
    "A Wild Last Boss Appeared" : "https://imgyx.pages.dev/HGBe4",
    "Black Clover" : "https://imgyx.pages.dev/PQ8rI",
    "Blue Box" : "https://imgyx.pages.dev/VnXBt",
    "Overgeared" : "https://imgyx.pages.dev/eOHjc",
    "Psyren" : "https://imgyx.pages.dev/8X1Oz",
    "Sword Art Online" : "https://imgyx.pages.dev/0fYcj",
    "As A Reincarnated Aristocrat I'll Use My Appraisal Skill To Rise The World" : "https://imgyx.pages.dev/IBNyv",
    "Even Though I'm a Super Timid Noble Girl, I Accepted the Bet From My Cunning Fiancé" : "https://imgyx.pages.dev/KhSAr",
}
DEFAULT_POSTER_URL = ""

# ==================== HEALTH CHECK SERVER ====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Anime Auto Post Bot is running!")

    def log_message(self, format_string, *args):
        return


def start_web_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info("Health server listening on port %s", PORT)
        server.serve_forever()
    except Exception:
        logger.exception("Health server failed to start")


# ==================== HELPERS ====================
def normalize_channel_id(value):
    value = value.strip()
    try:
        return str(int(value))
    except ValueError:
        return value.lstrip("@").lower()


def source_is_allowed(chat):
    configured = {normalize_channel_id(item) for item in SOURCE_CHANNEL_IDS}
    if str(chat.id) in configured:
        return True
    username = getattr(chat, "username", None)
    return bool(username and normalize_channel_id(username) in configured)


def contains_episode(text):
    return bool(re.search(r"\bepisode\b", text, flags=re.IGNORECASE))


def get_source_link(message):
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id = str(chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.message_id}"
    return None


def get_message_text(message):
    return message.text or message.caption or ""


def get_matching_value(mapping, anime_name):
    normalized = anime_name.lower().strip()
    for title, value in mapping.items():
        title_normalized = title.lower().strip()
        if title_normalized in normalized or normalized in title_normalized:
            return value
    return None


def get_anime_channel_link(anime_name):
    return get_matching_value(ANIME_CHANNEL_LINKS, anime_name) or DEFAULT_CHANNEL_LINK


def get_anime_poster_url(anime_name):
    """Database poster overrides take precedence over built-in defaults."""
    try:
        with closing(connect_db()) as conn:
            rows = conn.execute(
                "SELECT anime_name, poster_url FROM anime_posters ORDER BY length(anime_name) DESC"
            ).fetchall()
        normalized = normalize_name(anime_name)
        for row in rows:
            key = normalize_name(row["anime_name"])
            if key and (key in normalized or normalized in key):
                return row["poster_url"]
    except Exception:
        logger.exception("Could not read poster configuration from database")
    return get_matching_value(ANIME_POSTERS, anime_name) or get_setting("default_poster_url") or DEFAULT_POSTER_URL or None


def extract_episode_details(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    anime_name = "Unknown Anime"
    episode_number = "Unknown"
    season_number = "01"

    for line in lines:
        episode_match = re.search(
            r"\bEpisode\s*[:#-]?\s*(\d+(?:\.\d+)?)",
            line,
            re.IGNORECASE,
        )

        if episode_match:
            episode_number = episode_match.group(1)

        season_match = re.search(
            r"\bSeason\s*[:#-]?\s*(\d+)",
            line,
            re.IGNORECASE,
        )

        if season_match:
            season_number = season_match.group(1)

    for index, line in enumerate(lines):
        if re.search(r"\bEpisode\b", line, re.IGNORECASE):
            if index > 0:
                candidate = lines[index - 1]

                if not re.search(
                    r"\b(480p|720p|1080p|2160p|Tamil|English|Japanese|Season)\b",
                    candidate,
                    re.IGNORECASE,
                ):
                    anime_name = candidate
            break

    if anime_name == "Unknown Anime":
        for line in lines:
            if re.search(r"\bEpisode\b", line, re.IGNORECASE):
                continue

            if re.search(
                r"\b(480p|720p|1080p|2160p|Tamil|English|Japanese)\b",
                line,
                re.IGNORECASE,
            ):
                continue

            if re.search(
                r"\bSeason\s*[:#-]?\s*\d+",
                line,
                re.IGNORECASE,
            ):
                continue

            anime_name = line
            break

    return anime_name, episode_number, season_number


def connect_db():
    # Ensure the database directory exists and is writable.
    folder = os.path.dirname(os.path.abspath(DATABASE_PATH))
    os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(connect_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anime_routes (
                anime_key TEXT PRIMARY KEY,
                anime_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anime_posters (
                anime_key TEXT PRIMARY KEY,
                anime_name TEXT NOT NULL,
                poster_url TEXT NOT NULL
            )
        """)
        conn.commit()

def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()

def get_setting(key):
    with closing(connect_db()) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    with closing(connect_db()) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()

def get_route(anime_name):
    with closing(connect_db()) as conn:
        return conn.execute(
            "SELECT anime_name, channel_id FROM anime_routes WHERE anime_key = ?",
            (normalize_name(anime_name),),
        ).fetchone()

def parse_channel_id(value):
    value = value.strip()
    if not re.fullmatch(r"-?\d+", value):
        raise ValueError(
            "Channel must be a numeric Telegram channel ID, e.g. -1001234567890."
        )
    return int(value)

def parse_post(text):
    """Parse anime name, season, episode, poster URL, and quality links."""

    if not text:
        return None

    raw_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if len(raw_lines) < 2:
        return None

    image_re = re.compile(
        r"^\s*(?:image(?:\s*url)?|poster|thumbnail|photo)\s*[:=\-]?\s*"
        r"(https?://\S+)\s*$",
        re.IGNORECASE,
    )

    image_url = None
    lines = []

    for line in raw_lines:
        image_match = image_re.match(line)

        if image_match:
            candidate = image_match.group(1).rstrip(".,;)")
            parsed_image = urlparse(candidate)

            if parsed_image.scheme in ("http", "https") and parsed_image.netloc:
                image_url = candidate

            continue

        lines.append(line)

    if len(lines) < 2:
        return None

    # Detect season number.
    season_re = re.compile(
        r"\b(?:season|s)\s*[-:#]?\s*(\d+)\b",
        re.IGNORECASE,
    )

    season_number = "1"

    for line in lines:
        season_match = season_re.search(line)

        if season_match:
            season_number = season_match.group(1)
            break

    # Detect episode number.
    episode_re = re.compile(
        r"\b(?:(?:season|s)\s*\d+\s*[-:]?\s*)?"
        r"(?:episode|ep|e)\s*[-:#]?\s*(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    )

    episode_index = None
    episode_number = None

    for index, line in enumerate(lines):
        match = episode_re.search(line)

        if match:
            episode_index = index
            episode_number = match.group(1)
            break

    if episode_index is None:
        return None

    # Extract anime name and format it using title().
    anime_name = " ".join(
        lines[:episode_index]
    ).strip(" \t-–—:|")

    # Remove season information from the anime name if it appears there.
    anime_name = season_re.sub("", anime_name).strip(" \t-–—:|")

    if not anime_name:
        return None

    anime_name = anime_name.title()

    # Detect quality links.
    qualities = {}

    quality_re = re.compile(
        r"\b(144|240|360|480|540|576|720|1080|1440|2160)\s*[pP]\b"
        r"\s*[-:–—]?\s*(https?://[^\s|<>]+)",
        re.IGNORECASE,
    )

    for line in lines[episode_index + 1:]:
        for match in quality_re.finditer(line):
            quality = f"{match.group(1)}P"
            url = match.group(2).rstrip(".,;)")

            parsed_url = urlparse(url)

            if parsed_url.scheme in ("http", "https") and parsed_url.netloc:
                qualities[quality] = url

    # Also accept quality links on the episode-heading line.
    if not qualities:
        for line in lines[episode_index:]:
            for match in quality_re.finditer(line):
                quality = f"{match.group(1)}P"
                url = match.group(2).rstrip(".,;)")

                parsed_url = urlparse(url)

                if parsed_url.scheme in ("http", "https") and parsed_url.netloc:
                    qualities[quality] = url

    if not qualities:
        return None

    return {
        "anime_name": anime_name,
        "season_number": season_number,
        "episode_number": episode_number,
        "qualities": qualities,
        "image_url": image_url,
    }

def make_caption(anime_name, season_number, episode_number):
    safe_name = html.escape(anime_name.title())
    safe_season = html.escape(str(season_number).zfill(2))
    safe_episode = html.escape(str(episode_number))

    return (
        f"{safe_name} | தமிழ் Official Dub\n\n"
        f"Season : {safe_season} | Episode : {safe_episode}\n\n"
        "‼️ Note - Click The Below Button to Get Episodes 👇"
    )

def make_keyboard(qualities):
    # Buttons are arranged two per row.
    preferred = [
        "480P", "720P", "1080P", "360P", "240P",
        "144P", "540P", "576P", "1440P", "2160P",
    ]
    ordered = [quality for quality in preferred if quality in qualities]
    ordered.extend(quality for quality in qualities if quality not in ordered)

    buttons = [
        InlineKeyboardButton(f"📥 {quality}", url=qualities[quality])
        for quality in ordered
    ]
    rows = [
        buttons[index:index + 2]
        for index in range(0, len(buttons), 2)
    ]
    return InlineKeyboardMarkup(rows)

async def is_admin(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return False

    if not ADMIN_IDS:
        await message.reply_text(
            "⚠️ ADMIN_IDS is not configured. Add your Telegram numeric user ID "
            "in Render environment variables."
        )
        return False

    if user.id not in ADMIN_IDS:
        await message.reply_text(
            "⛔ This command is restricted to the bot admin."
        )
        return False

    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🔥 Anime Auto Publisher is running!\n\n"
        "Commands:\n"
        "/addanime Anime Name -1001234567890\n"
        "/removeanime Anime Name\n"
        "/listanime\n"
        "/setdefault -1001234567890\n"
        "/defaultchannel\n"
        "/setposter Anime Name https://image-url\n"
        "/removeposter Anime Name\n"
        "/listposters\n"
        "/setdefaultposter https://image-url (or off)\n"
        "/help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def addanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return

    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/addanime Anime Name -1001234567890\n\n"
            "Put the channel ID last. The bot must be an administrator in that channel."
        )
        return

    try:
        channel_id = parse_channel_id(args[-1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    anime_name = " ".join(args[:-1]).strip()
    if not anime_name:
        await update.effective_message.reply_text("Please provide an anime name.")
        return

    try:
        chat = await context.bot.get_chat(channel_id)

        with closing(connect_db()) as conn:
            conn.execute(
                "INSERT INTO anime_routes(anime_key, anime_name, channel_id) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(anime_key) DO UPDATE SET "
                "anime_name=excluded.anime_name, "
                "channel_id=excluded.channel_id",
                (normalize_name(anime_name), anime_name, channel_id),
            )
            conn.commit()

        await update.effective_message.reply_text(
            f"✅ Registered: {anime_name}\n"
            f"📢 Destination: {chat.title or channel_id}\n"
            f"🆔 ID: {channel_id}"
        )
    except Exception as exc:
        logger.exception("Failed to register route")
        await update.effective_message.reply_text(
            "❌ Couldn't access that channel. Make sure the bot is an administrator "
            "there and that the numeric channel ID is correct.\n"
            f"Details: {exc}"
        )

async def removeanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return

    anime_name = " ".join(context.args).strip()
    if not anime_name:
        await update.effective_message.reply_text(
            "Usage: /removeanime Anime Name"
        )
        return

    with closing(connect_db()) as conn:
        cursor = conn.execute(
            "DELETE FROM anime_routes WHERE anime_key = ?",
            (normalize_name(anime_name),),
        )
        conn.commit()

    if cursor.rowcount:
        await update.effective_message.reply_text(
            f"🗑️ Removed route for: {anime_name}"
        )
    else:
        await update.effective_message.reply_text(
            f"ℹ️ No route found for: {anime_name}"
        )

async def listanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return

    with closing(connect_db()) as conn:
        rows = conn.execute(
            "SELECT anime_name, channel_id FROM anime_routes "
            "ORDER BY anime_name COLLATE NOCASE"
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "No anime routes registered yet."
        )
        return

    lines = ["📚 Registered anime channels:"]
    for row in rows:
        lines.append(f"• {row['anime_name']} — `{row['channel_id']}`")

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )

async def setdefault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /setdefault -1001234567890"
        )
        return

    try:
        channel_id = parse_channel_id(context.args[0])
        chat = await context.bot.get_chat(channel_id)
        set_setting("default_channel_id", channel_id)

        await update.effective_message.reply_text(
            f"✅ Default channel set to {chat.title or channel_id}\n"
            f"🆔 {channel_id}"
        )
    except Exception as exc:
        logger.exception("Failed to set default channel")
        await update.effective_message.reply_text(
            "❌ Couldn't access that channel. Add the bot as an administrator "
            "and check the ID.\n"
            f"Details: {exc}"
        )

async def defaultchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return

    channel_id = get_setting("default_channel_id")
    if not channel_id:
        await update.effective_message.reply_text(
            "No default channel configured. Use /setdefault -1001234567890"
        )
        return

    try:
        chat = await context.bot.get_chat(int(channel_id))
        title = chat.title or str(channel_id)
    except Exception:
        title = "Unable to fetch channel title"

    await update.effective_message.reply_text(
        f"📢 Default channel: {title}\n🆔 {channel_id}"
    )

def validate_http_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Please provide a valid http:// or https:// URL.")
    return value

async def setposter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /setposter Anime Name https://direct-image-url"""
    if not await is_admin(update):
        return
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setposter Anime Name https://example.com/poster.jpg\n"
            "Use a direct image URL that Telegram can fetch."
        )
        return
    try:
        poster_url = validate_http_url(args[-1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    anime_name = " ".join(args[:-1]).strip()
    if not anime_name:
        await update.effective_message.reply_text("Please provide an anime name.")
        return
    try:
        with closing(connect_db()) as conn:
            conn.execute(
                "INSERT INTO anime_posters(anime_key, anime_name, poster_url) VALUES(?, ?, ?) "
                "ON CONFLICT(anime_key) DO UPDATE SET anime_name=excluded.anime_name, poster_url=excluded.poster_url",
                (normalize_name(anime_name), anime_name, poster_url),
            )
            conn.commit()
        logger.info("Admin %s updated poster URL for %s", update.effective_user.id, anime_name)
        await update.effective_message.reply_text(
            f"✅ Poster saved for {anime_name}.\nURL: {poster_url}\n"
            "The custom poster takes priority over the built-in poster."
        )
    except Exception:
        logger.exception("Failed to save poster for %s", anime_name)
        await update.effective_message.reply_text("❌ Could not save the poster. Check the database and Render logs.")

async def removeposter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    anime_name = " ".join(context.args).strip()
    if not anime_name:
        await update.effective_message.reply_text("Usage: /removeposter Anime Name")
        return
    try:
        with closing(connect_db()) as conn:
            cursor = conn.execute("DELETE FROM anime_posters WHERE anime_key = ?", (normalize_name(anime_name),))
            conn.commit()
        if cursor.rowcount:
            await update.effective_message.reply_text(f"🗑️ Removed custom poster for {anime_name}. Built-in poster will be used if available.")
            logger.info("Admin %s removed poster override for %s", update.effective_user.id, anime_name)
        else:
            await update.effective_message.reply_text(f"ℹ️ No custom poster saved for {anime_name}.")
    except Exception:
        logger.exception("Failed to remove poster for %s", anime_name)
        await update.effective_message.reply_text("❌ Could not remove the poster. Check the Render logs.")

async def listposters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    try:
        with closing(connect_db()) as conn:
            rows = conn.execute("SELECT anime_name, poster_url FROM anime_posters ORDER BY anime_name COLLATE NOCASE").fetchall()
        if not rows:
            await update.effective_message.reply_text("No custom posters saved yet. Use /setposter Anime Name https://image-url")
            return
        lines = ["🖼️ Custom anime posters:"]
        for row in rows:
            lines.append(f"• {row['anime_name']}\n{row['poster_url']}")
        message = "\n\n".join(lines)
        # Keep each Telegram message under its maximum size.
        for offset in range(0, len(message), 3500):
            await update.effective_message.reply_text(message[offset:offset + 3500], disable_web_page_preview=True)
    except Exception:
        logger.exception("Failed to list custom posters")
        await update.effective_message.reply_text("❌ Could not read poster settings. Check the Render logs.")

async def setdefaultposter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefaultposter https://example.com/poster.jpg\nUse /setdefaultposter off to disable the default poster.")
        return
    value = context.args[0].strip()
    if value.casefold() == "off":
        set_setting("default_poster_url", "")
        await update.effective_message.reply_text("✅ Default poster disabled.")
        return
    try:
        value = validate_http_url(value)
        set_setting("default_poster_url", value)
        await update.effective_message.reply_text("✅ Default poster URL saved.")
        logger.info("Admin %s updated default poster URL", update.effective_user.id)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
    except Exception:
        logger.exception("Failed to set default poster")
        await update.effective_message.reply_text("❌ Could not save the default poster. Check the Render logs.")

async def process_source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    post_text = message.text or message.caption or ""
    if not post_text:
        return

    if not SOURCE_CHANNEL_ID:
        logger.error("SOURCE_CHANNEL_ID is not configured")
        return

    try:
        expected_source_id = int(SOURCE_CHANNEL_ID)
    except ValueError:
        logger.error("SOURCE_CHANNEL_ID must be a numeric channel ID")
        return

    if (
        not update.effective_chat
        or update.effective_chat.id != expected_source_id
    ):
        return

    parsed = parse_post(post_text)
    if not parsed:
        logger.info(
            "Ignored source post: did not match anime/episode/quality-link format"
        )
        return

    anime_name = parsed["anime_name"]
    route = get_route(anime_name)

    if route:
        destination_id = int(route["channel_id"])
        destination_name = route["anime_name"]
    else:
        default_id = get_setting("default_channel_id")
        if not default_id:
            logger.warning(
                "No route for %s and no default channel configured",
                anime_name,
            )
            return
        destination_id = int(default_id)
        destination_name = "default channel"

    caption = make_caption(
    anime_name,
    parsed["season_number"],
    parsed["episode_number"],)

    keyboard = make_keyboard(parsed["qualities"])
    image_url = parsed.get("image_url")

    try:
        if image_url:
            try:
                await context.bot.send_photo(
                    chat_id=destination_id,
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception:
                # If Telegram cannot load the supplied image URL, still publish the links.
                logger.exception(
                    "Photo publication failed; falling back to a text post"
                )
                await context.bot.send_message(
                    chat_id=destination_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
        else:
            await context.bot.send_message(
                chat_id=destination_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        METRICS["upload_posts_ok"] += 1
        logger.info(
            "Published %s Episode %s to %s (photo=%s)",
            anime_name,
            parsed["episode_number"],
            destination_name,
            bool(image_url),
        )
    except Exception:
        METRICS["upload_posts_failed"] += 1
        logger.exception(
            "Failed to publish %s to channel %s",
            anime_name,
            destination_id,
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error:
        logger.error(
            "Unhandled Telegram update error (update=%r)",
            update,
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Telegram handler reported an unknown error (update=%r)", update)

# ==================== COMMANDS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "👋 Welcome to Anime Auto Post Bot!\n\n"
            "I detect channel posts containing the word Episode.\n"
            "Matching posts are announced in your main channel.\n\n"
            "Each anime can have its own channel link and poster.\n\n"
            "Use /status to check the bot.\n\n"
            "Upload routing: /addanime, /removeanime, /listanime, /setdefault, /defaultchannel\n"
            "Poster tools (admin): /setposter, /removeposter, /listposters, /setdefaultposter"
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    try:
        with closing(connect_db()) as conn:
            conn.execute("SELECT 1").fetchone()
            route_count = conn.execute("SELECT COUNT(*) FROM anime_routes").fetchone()[0]
            poster_count = conn.execute("SELECT COUNT(*) FROM anime_posters").fetchone()[0]
        db_state = "✅ Connected"
    except Exception as exc:
        logger.exception("Status check: database unavailable")
        route_count = poster_count = 0
        db_state = f"❌ Unavailable ({type(exc).__name__})"
    uptime_seconds = max(0, int(time.time() - STARTED_AT))
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    is_admin_user = bool(update.effective_user and update.effective_user.id in ADMIN_IDS)
    lines = [
        "🤖 Combined Anime Bot Status",
        "🟢 Process: Running",
        f"⏱️ Uptime: {hours}h {minutes}m {seconds}s",
        f"📢 Auto-post source channels: {len(SOURCE_CHANNEL_IDS)}",
        f"🎬 Upload source configured: {'Yes' if SOURCE_CHANNEL_ID else 'No'}",
        f"🗄️ SQLite: {db_state}",
        f"🧭 Anime routes: {route_count}",
        f"🖼️ Custom posters: {poster_count}",
        f"📤 Episode announcements sent: {METRICS['episode_posts_ok']}",
        f"⚠️ Episode announcement failures: {METRICS['episode_posts_failed']}",
        f"📤 Upload posts sent: {METRICS['upload_posts_ok']}",
        f"⚠️ Upload post failures: {METRICS['upload_posts_failed']}",
    ]
    if is_admin_user:
        lines.append(f"🧾 Database path: {DATABASE_PATH}")
        lines.append(f"👮 Admin IDs configured: {len(ADMIN_IDS)}")
    await update.effective_message.reply_text("\n".join(lines))


# ==================== EPISODE DETECTION ====================
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source_message = update.channel_post
    if not source_message:
        return

    if not source_is_allowed(source_message.chat):
        logger.info(
            "Ignoring post from unconfigured source channel %s",
            source_message.chat.id,
        )
        return

    original_text = get_message_text(source_message)
    if not original_text or not contains_episode(original_text):
        return

    anime_name, episode_number, season_number = extract_episode_details(original_text)
    anime_link = get_anime_channel_link(anime_name)
    poster_url = get_anime_poster_url(anime_name)

    announcement = (
        f"<blockquote><b>➪ {escape(anime_name)}</b></blockquote>\n"
    
    "──────────────────\n"
    f"<b>➥ Season : {escape(season_number)}\n"   
    f"➤ Episode: {escape(episode_number)}\n"
    "➥ Quality: Multi Quality [Tamil Audio]</b>\n"
    "──────────────────\n"
    f"<blockquote><b>➽ NEW EPISODE {escape(episode_number)} ADDED 👇</b></blockquote>" )
  

    # Telegram Bot API does not support custom inline-button background colors.
    # This layout matches the screenshot: one full-width row, then two side-by-side.
    buttons = [
    [
        InlineKeyboardButton(
            "⎙• ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴊᴏɪɴ •⎙",
            url=anime_link,
            style="success",
        )
    ],
    [
        InlineKeyboardButton(
            "↪ ꜱʜᴀʀᴇ ᴄʜᴀɴɴᴇʟ",
            url="https://t.me/AnimeNetworkTamil",
            style="primary",
        ),
        InlineKeyboardButton(
            "⚠️ ᴀɴʏ ɪꜱꜱᴜᴇꜱ ⚠️",
            url="https://t.me/Tamil_Animes_New",
            style="danger",
        ),
    ],
]

    keyboard = InlineKeyboardMarkup(buttons)

    try:
        if poster_url:
            try:
                await context.bot.send_photo(
                    chat_id=MAIN_CHANNEL_ID,
                    photo=poster_url,
                    caption=announcement,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                logger.info("Posted episode with poster for %s", anime_name)
            except Exception:
                logger.warning(
                    "Could not send poster for %s; falling back to text. "
                    "Check that the URL is a direct image URL.",
                    anime_name,
                    exc_info=True,
                )
                await context.bot.send_message(
                    chat_id=MAIN_CHANNEL_ID,
                    text=announcement,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
        else:
            logger.warning("No poster URL configured for anime: %s", anime_name)
            await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=announcement,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        METRICS["episode_posts_ok"] += 1
        logger.info(
            "Episode %s announcement sent for %s",
            episode_number,
            anime_name,
        )
    except Exception:
        METRICS["episode_posts_failed"] += 1
        logger.exception(
            "Failed to publish episode announcement to MAIN_CHANNEL_ID=%s",
            MAIN_CHANNEL_ID,
        )



# ==================== COMBINED MAIN ====================
def main():
    # Initialize ANuploadvot's SQLite routing database.
    init_db()

    Thread(target=start_web_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    # Shared commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))

    # ANuploadvot route-management commands
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addanime", addanime))
    application.add_handler(CommandHandler("removeanime", removeanime))
    application.add_handler(CommandHandler("listanime", listanime))
    application.add_handler(CommandHandler("setdefault", setdefault))
    application.add_handler(CommandHandler("defaultchannel", defaultchannel))
    application.add_handler(CommandHandler("setposter", setposter))
    application.add_handler(CommandHandler("removeposter", removeposter))
    application.add_handler(CommandHandler("listposters", listposters))
    application.add_handler(CommandHandler("setdefaultposter", setdefaultposter))

    # Auto-post announcements from every channel in SOURCE_CHANNEL_IDS.
    channel_text_or_caption = filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION)
    application.add_handler(
        MessageHandler(channel_text_or_caption, handle_channel_post)
    )

    # Upload/quality-link publisher from SOURCE_CHANNEL_ID.
    # This handler supports text posts and captions on photo/video/document posts.
    application.add_handler(
        MessageHandler(channel_text_or_caption, process_source_post),
        group=1,
    )

    application.add_error_handler(error_handler)

    logger.info("Starting combined Auto-post-Bot + ANuploadvot")
    application.run_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
