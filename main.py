import os
import re
import time
import html
import logging
import sqlite3
from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from urllib.parse import urlparse

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

STARTED_AT = time.time()

METRICS = {
    "episode_posts_ok": 0,
    "episode_posts_failed": 0,
    "upload_posts_ok": 0,
    "upload_posts_failed": 0,
}

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "").strip()

SOURCE_CHANNEL_IDS = [
    x.strip()
    for x in os.getenv("SOURCE_CHANNEL_IDS", "").split(",")
    if x.strip()
]

SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "anime_auto_post_bot").strip()

PORT = int(os.getenv("PORT", "10000"))

# Used only when importing old SQLite records.
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/var/data/anime_channels.sqlite3"
).strip()

MIGRATE_SQLITE = (
    os.getenv("MIGRATE_SQLITE", "true").strip().lower()
    in ("true", "1", "yes")
)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing!")

if not MAIN_CHANNEL_ID:
    raise ValueError("MAIN_CHANNEL_ID is missing!")

if not SOURCE_CHANNEL_IDS:
    raise ValueError("SOURCE_CHANNEL_IDS is missing!")

if not MONGODB_URI:
    raise ValueError("MONGODB_URI is missing!")

# =========================================================
# BUILT-IN ANIME CHANNEL LINKS
# =========================================================

ANIME_CHANNEL_LINKS = {
    "naruto shippuden": "https://t.me/+uKuAaVBzmSs0ZDll",
    "liar game": "https://t.me/+ki2AYsqJafs2NDk1",
    "the laid-off cheat-granting mage enjoys a second lease on life": "https://t.me/+Sij9-LGXOmFjYTA1",
    "tomb raider king": "https://t.me/+1d2421FenfNlZGM9",
    "the exiled heavy knight knows how to game the system": "https://t.me/+SppUueMG-bdiMjI1",
    "the ramparts of ice": "https://t.me/+eGLMJuQ2lTA4YTBl",
    "jojo’s bizarre adventure steel ball run": "https://t.me/+4iT4nt1SFNE2Yzdl",
    "the apothecary diaries": "https://t.me/+Hdz90USLCW4yZjE1",
    "a wild last boss appeared": "https://t.me/+s6_zOzbnJ_UxMjk9",
    "black clover": "https://t.me/+WTEdJxxK8OU0ODBl",
    "blue box": "https://t.me/+NNlAczU_448zZTI1",
    "psyren": "https://t.me/+qb7j7gch9rg5NjM1",
    "overgeared": "https://t.me/+mzi8whxY721mZmM1",
    "sword art online": "https://t.me/+4WesuhOsbsUwNmM1",
    "firefly wedding": "https://t.me/+Rse64X9j-idlYjRl",
    "as a reincarnated aristocrat i'll use my appraisal skill to rise the world": "https://t.me/+k2ETQ1m_J3lhZmQ1",
    "even though i'm a super timid noble girl, i accepted the bet from my cunning fiancé": "https://t.me/+iperfm4Spv0wMzI9",
    "demon slayer": "https://t.me/DemonSlayerChannel",
}

DEFAULT_CHANNEL_LINK = "https://t.me/AnimeNetworkTamil"

# =========================================================
# BUILT-IN POSTERS
# =========================================================

ANIME_POSTERS = {
    "naruto shippuden": "https://imgyx.pages.dev/VQw3e",
    "liar game": "https://imgyx.pages.dev/sjYWT",
    "the laid-off cheat-granting mage enjoys a second lease on life": "https://imgyx.pages.dev/67la6",
    "tomb raider king": "https://imgyx.pages.dev/gKj9J",
    "the exiled heavy knight knows how to game the system": "https://imgyx.pages.dev/xNjX5",
    "the ramparts of ice": "https://imgyx.pages.dev/cckx3",
    "jojo’s bizarre adventure steel ball run": "https://imgyx.pages.dev/NDUnT",
    "the apothecary diaries": "https://imgyx.pages.dev/JaNj9",
    "a wild last boss appeared": "https://imgyx.pages.dev/HGBe4",
    "black clover": "https://imgyx.pages.dev/PQ8rI",
    "blue box": "https://imgyx.pages.dev/VnXBt",
    "overgeared": "https://imgyx.pages.dev/eOHjc",
    "psyren": "https://imgyx.pages.dev/8X1Oz",
    "sword art online": "https://imgyx.pages.dev/0fYcj",
    "as a reincarnated aristocrat i'll use my appraisal skill to rise the world": "https://imgyx.pages.dev/IBNyv",
    "even though i'm a super timid noble girl, i accepted the bet from my cunning fiancé": "https://imgyx.pages.dev/KhSAr",
}

DEFAULT_POSTER_URL = ""

# =========================================================
# MONGODB
# =========================================================

mongo_client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    retryWrites=True,
)

db = mongo_client[MONGODB_DATABASE]

routes_col = db["anime_routes"]
settings_col = db["bot_settings"]
posters_col = db["anime_posters"]
links_col = db["anime_links"]
history_col = db["episode_history"]
logs_col = db["admin_logs"]


def normalize_name(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def init_db():
    """Connect to MongoDB and create indexes."""

    mongo_client.admin.command("ping")

    routes_col.create_index(
        [("anime_key", ASCENDING)],
        unique=True
    )

    posters_col.create_index(
        [("anime_key", ASCENDING)],
        unique=True
    )

    links_col.create_index(
        [("anime_key", ASCENDING)],
        unique=True
    )

    history_col.create_index(
        [("created_at", ASCENDING)]
    )

    logs_col.create_index(
        [("created_at", ASCENDING)]
    )

    logger.info("MongoDB connected successfully.")


def migrate_sqlite():
    """Import existing SQLite data into MongoDB without deleting SQLite."""

    if not MIGRATE_SQLITE:
        logger.info("SQLite migration disabled.")
        return

    if not os.path.isfile(DATABASE_PATH):
        logger.info("No old SQLite database found. Skipping migration.")
        return

    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row

            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            if "anime_routes" in tables:
                for row in conn.execute(
                    "SELECT anime_key, anime_name, channel_id FROM anime_routes"
                ):
                    routes_col.update_one(
                        {"anime_key": row["anime_key"]},
                        {"$set": {
                            "anime_key": row["anime_key"],
                            "anime_name": row["anime_name"],
                            "channel_id": int(row["channel_id"]),
                        }},
                        upsert=True,
                    )

            if "anime_posters" in tables:
                for row in conn.execute(
                    "SELECT anime_key, anime_name, poster_url FROM anime_posters"
                ):
                    posters_col.update_one(
                        {"anime_key": row["anime_key"]},
                        {"$set": {
                            "anime_key": row["anime_key"],
                            "anime_name": row["anime_name"],
                            "poster_url": row["poster_url"],
                        }},
                        upsert=True,
                    )

            if "anime_links" in tables:
                for row in conn.execute(
                    "SELECT anime_key, anime_name, channel_link FROM anime_links"
                ):
                    links_col.update_one(
                        {"anime_key": row["anime_key"]},
                        {"$set": {
                            "anime_key": row["anime_key"],
                            "anime_name": row["anime_name"],
                            "channel_link": row["channel_link"],
                        }},
                        upsert=True,
                    )

            if "settings" in tables:
                for row in conn.execute(
                    "SELECT key, value FROM settings"
                ):
                    settings_col.update_one(
                        {"_id": row["key"]},
                        {"$set": {"value": str(row["value"])}},
                        upsert=True,
                    )

        logger.info("SQLite to MongoDB migration completed.")

    except Exception:
        logger.exception("SQLite migration failed.")
        raise


def get_setting(key, default=None):
    doc = settings_col.find_one({"_id": key})
    return doc.get("value", default) if doc else default


def set_setting(key, value):
    settings_col.update_one(
        {"_id": key},
        {"$set": {"value": str(value)}},
        upsert=True,
    )


def log_admin_action(user_id, action, details=""):
    try:
        logs_col.insert_one({
            "user_id": user_id,
            "action": action,
            "details": str(details),
            "created_at": time.time(),
        })
    except Exception:
        logger.exception("Could not save admin log.")


# =========================================================
# MATCHING HELPERS
# =========================================================

def get_matching_value(mapping, anime_name):
    normalized = normalize_name(anime_name)

    # Exact match first.
    if normalized in mapping:
        return mapping[normalized]

    # Then longest partial match.
    matches = [
        (normalize_name(title), value)
        for title, value in mapping.items()
        if normalize_name(title) in normalized
        or normalized in normalize_name(title)
    ]

    if matches:
        matches.sort(key=lambda item: len(item[0]), reverse=True)
        return matches[0][1]

    return None


def find_anime_document(collection, anime_name):
    normalized = normalize_name(anime_name)

    exact = collection.find_one({"anime_key": normalized})
    if exact:
        return exact

    docs = list(collection.find({}))

    matches = []

    for doc in docs:
        key = normalize_name(doc.get("anime_name", ""))

        if key and (key in normalized or normalized in key):
            matches.append(doc)

    if matches:
        matches.sort(
            key=lambda item: len(normalize_name(item.get("anime_name", ""))),
            reverse=True,
        )
        return matches[0]

    return None


def get_anime_channel_link(anime_name):
    doc = find_anime_document(links_col, anime_name)

    if doc:
        return doc["channel_link"]

    return (
        get_matching_value(ANIME_CHANNEL_LINKS, anime_name)
        or get_setting("default_channel_link")
        or DEFAULT_CHANNEL_LINK
    )


def get_anime_poster_url(anime_name):
    doc = find_anime_document(posters_col, anime_name)

    if doc:
        return doc["poster_url"]

    return (
        get_matching_value(ANIME_POSTERS, anime_name)
        or get_setting("default_poster_url")
        or DEFAULT_POSTER_URL
        or None
    )


def get_route(anime_name):
    return find_anime_document(routes_col, anime_name)


# =========================================================
# HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Anime Auto Post Bot is running!")

    def log_message(self, format_string, *args):
        return


def start_web_server():
    try:
        server = ThreadingHTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler
        )
        logger.info("Health server listening on %s", PORT)
        server.serve_forever()
    except Exception:
        logger.exception("Health server failed.")


# =========================================================
# GENERAL HELPERS
# =========================================================

def normalize_channel_id(value):
    value = value.strip()

    try:
        return str(int(value))
    except ValueError:
        return value.lstrip("@").lower()


def source_is_allowed(chat):
    configured = {
        normalize_channel_id(item)
        for item in SOURCE_CHANNEL_IDS
    }

    if str(chat.id) in configured:
        return True

    username = getattr(chat, "username", None)

    return bool(
        username
        and normalize_channel_id(username) in configured
    )


def contains_episode(text):
    return bool(
        re.search(r"\bepisode\b", text, re.IGNORECASE)
    )


def get_message_text(message):
    return message.text or message.caption or ""


def parse_channel_id(value):
    value = value.strip()

    if not re.fullmatch(r"-?\d+", value):
        raise ValueError(
            "Use a numeric Telegram channel ID, e.g. -1001234567890."
        )

    return int(value)


def validate_http_url(value):
    value = value.strip()
    parsed = urlparse(value)

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
    ):
        raise ValueError("Please provide a valid HTTP or HTTPS URL.")

    return value


def validate_telegram_link(value):
    value = validate_http_url(value)
    parsed = urlparse(value)

    if parsed.hostname not in (
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    ):
        raise ValueError("Please provide a Telegram channel link.")

    return value


async def is_admin(update):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return False

    if not ADMIN_IDS:
        await message.reply_text(
            "⚠️ ADMIN_IDS is not configured in Render."
        )
        return False

    if user.id not in ADMIN_IDS:
        await message.reply_text(
            "⛔ This command is restricted to the bot admin."
        )
        return False

    return True


# =========================================================
# UPLOAD POST PARSER
# =========================================================

def parse_post(text):
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
        r"^\s*(?:image(?:\s*url)?|poster|thumbnail|photo)"
        r"\s*[:=\-]?\s*(https?://\S+)\s*$",
        re.IGNORECASE,
    )

    image_url = None
    lines = []

    for line in raw_lines:
        match = image_re.match(line)

        if match:
            candidate = match.group(1).rstrip(".,;)")
            parsed = urlparse(candidate)

            if parsed.scheme in ("http", "https") and parsed.netloc:
                image_url = candidate

            continue

        lines.append(line)

    if len(lines) < 2:
        return None

    season_re = re.compile(
        r"\b(?:season|s)\s*[-:#]?\s*(\d+)\b",
        re.IGNORECASE,
    )

    season_number = "1"

    for line in lines:
        match = season_re.search(line)

        if match:
            season_number = match.group(1)
            break

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

    anime_name = " ".join(lines[:episode_index])
    anime_name = anime_name.strip(" \t-–—:|")
    anime_name = season_re.sub("", anime_name).strip(" \t-–—:|")

    if not anime_name:
        return None

    anime_name = anime_name.title()

    quality_re = re.compile(
        r"\b(144|240|360|480|540|576|720|1080|1440|2160)\s*[pP]\b"
        r"\s*[-:–—]?\s*(https?://[^\s|<>]+)",
        re.IGNORECASE,
    )

    qualities = {}

    for line in lines[episode_index:]:
        for match in quality_re.finditer(line):
            quality = f"{match.group(1)}P"
            url = match.group(2).rstrip(".,;)")

            parsed = urlparse(url)

            if parsed.scheme in ("http", "https") and parsed.netloc:
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


# =========================================================
# UPLOAD CAPTION AND BUTTONS
# =========================================================

def make_caption(anime_name, season_number, episode_number):
    safe_name = html.escape(anime_name)
    safe_season = html.escape(str(season_number).zfill(2))
    safe_episode = html.escape(str(episode_number))

    return (
        f"{safe_name} | தமிழ் Official Dub\n\n"
        f"Season : {safe_season} | Episode : {safe_episode}\n\n"
        "‼️ Note - Click The Below Button to Get Episodes 👇"
    )


def make_keyboard(qualities):
    preferred = [
        "480P", "720P", "1080P", "360P", "240P",
        "144P", "540P", "576P", "1440P", "2160P",
    ]

    ordered = [
        q for q in preferred if q in qualities
    ]

    ordered.extend(
        q for q in qualities if q not in ordered
    )

    buttons = [
        InlineKeyboardButton(
            f"📥 {quality}",
            url=qualities[quality]
        )
        for quality in ordered
    ]

    rows = [
        buttons[i:i + 2]
        for i in range(0, len(buttons), 2)
    ]

    return InlineKeyboardMarkup(rows)


# =========================================================
# DUPLICATE PROTECTION
# =========================================================

def reserve_episode(key, data):
    """
    Insert a unique record before publishing.
    A duplicate key means the episode was already processed.
    """

    document = {
        "_id": key,
        **data,
        "status": "pending",
        "created_at": time.time(),
    }

    try:
        history_col.insert_one(document)
        return True

    except DuplicateKeyError:
        return False


def mark_episode_sent(key, telegram_message_id=None):
    history_col.update_one(
        {"_id": key},
        {
            "$set": {
                "status": "sent",
                "telegram_message_id": telegram_message_id,
                "sent_at": time.time(),
            }
        },
    )


def release_episode(key):
    """Allow a failed publication to be retried."""

    history_col.delete_one({
        "_id": key,
        "status": "pending",
    })


def make_upload_key(anime_name, season, episode):
    return (
        "upload:"
        + normalize_name(anime_name)
        + f":s{season}:e{episode}"
    )


def make_announcement_key(chat_id, message_id):
    return f"announcement:{chat_id}:{message_id}"


# =========================================================
# ADMIN COMMANDS - ROUTES
# =========================================================

async def addanime(update, context):
    if not await is_admin(update):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/addanime Anime Name -1001234567890"
        )
        return

    try:
        channel_id = parse_channel_id(context.args[-1])
        anime_name = " ".join(context.args[:-1]).strip()

        chat = await context.bot.get_chat(channel_id)

        routes_col.update_one(
            {"anime_key": normalize_name(anime_name)},
            {"$set": {
                "anime_key": normalize_name(anime_name),
                "anime_name": anime_name,
                "channel_id": channel_id,
                "updated_at": time.time(),
            }},
            upsert=True,
        )

        log_admin_action(
            update.effective_user.id,
            "addanime",
            anime_name
        )

        await update.effective_message.reply_text(
            f"✅ Anime route saved!\n"
            f"🎬 {anime_name}\n"
            f"📢 {chat.title or channel_id}\n"
            f"🆔 {channel_id}"
        )

    except Exception as exc:
        logger.exception("Failed to add anime route")
        await update.effective_message.reply_text(
            f"❌ Could not add route.\n{exc}"
        )


async def removeanime(update, context):
    if not await is_admin(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.effective_message.reply_text(
            "Usage: /removeanime Anime Name"
        )
        return

    result = routes_col.delete_one({
        "anime_key": normalize_name(name)
    })

    await update.effective_message.reply_text(
        "🗑️ Route removed."
        if result.deleted_count
        else "ℹ️ No route found."
    )

    log_admin_action(update.effective_user.id, "removeanime", name)


async def listanime(update, context):
    if not await is_admin(update):
        return

    rows = list(
        routes_col.find({}).sort("anime_name", ASCENDING)
    )

    if not rows:
        await update.effective_message.reply_text(
            "No anime routes registered."
        )
        return

    lines = ["📚 REGISTERED ANIME ROUTES\n"]

    for row in rows:
        lines.append(
            f"🎬 {row['anime_name']}\n"
            f"🆔 {row['channel_id']}\n"
        )

    message = "\n".join(lines)

    for i in range(0, len(message), 3500):
        await update.effective_message.reply_text(
            message[i:i + 3500]
        )


async def setdefault(update, context):
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
            f"✅ Default upload channel set!\n"
            f"{chat.title or channel_id}\n{channel_id}"
        )

        log_admin_action(
            update.effective_user.id,
            "setdefault",
            channel_id
        )

    except Exception as exc:
        await update.effective_message.reply_text(
            f"❌ Could not set default channel.\n{exc}"
        )


async def defaultchannel(update, context):
    if not await is_admin(update):
        return

    channel_id = get_setting("default_channel_id")

    if not channel_id:
        await update.effective_message.reply_text(
            "No default channel. Use /setdefault."
        )
        return

    try:
        chat = await context.bot.get_chat(int(channel_id))
        title = chat.title or channel_id
    except Exception:
        title = channel_id

    await update.effective_message.reply_text(
        f"📢 Default channel: {title}\n🆔 {channel_id}"
    )


# =========================================================
# ADMIN COMMANDS - ANIME LINKS
# =========================================================

async def setanimelink(update, context):
    if not await is_admin(update):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/setanimelink Anime Name https://t.me/channel"
        )
        return

    name = " ".join(context.args[:-1]).strip()

    try:
        link = validate_telegram_link(context.args[-1])

        links_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name),
                "anime_name": name,
                "channel_link": link,
                "updated_at": time.time(),
            }},
            upsert=True,
        )

        await update.effective_message.reply_text(
            f"✅ Anime link saved!\n\n"
            f"🎬 {name}\n🔗 {link}"
        )

        log_admin_action(
            update.effective_user.id,
            "setanimelink",
            name
        )

    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def removeanimelink(update, context):
    if not await is_admin(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.effective_message.reply_text(
            "Usage: /removeanimelink Anime Name"
        )
        return

    result = links_col.delete_one({
        "anime_key": normalize_name(name)
    })

    await update.effective_message.reply_text(
        f"🗑️ Removed link for {name}."
        if result.deleted_count
        else f"ℹ️ No saved link for {name}."
    )


async def listanimelinks(update, context):
    if not await is_admin(update):
        return

    rows = list(
        links_col.find({}).sort("anime_name", ASCENDING)
    )

    if not rows:
        await update.effective_message.reply_text(
            "No custom anime links saved."
        )
        return

    lines = ["🎌 SAVED ANIME LINKS\n"]

    for row in rows:
        lines.append(
            f"🎬 {row['anime_name']}\n"
            f"🔗 {row['channel_link']}\n"
        )

    message = "\n".join(lines)

    for i in range(0, len(message), 3500):
        await update.effective_message.reply_text(
            message[i:i + 3500],
            disable_web_page_preview=True,
        )


async def getanimelink(update, context):
    if not await is_admin(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.effective_message.reply_text(
            "Usage: /getanimelink Anime Name"
        )
        return

    doc = find_anime_document(links_col, name)

    if doc:
        link = doc["channel_link"]
        source = "Custom MongoDB link"
    else:
        link = (
            get_matching_value(ANIME_CHANNEL_LINKS, name)
            or get_setting("default_channel_link")
            or DEFAULT_CHANNEL_LINK
        )
        source = "Built-in/default link"

    await update.effective_message.reply_text(
        f"🎬 Anime: {name}\n"
        f"🔗 Channel: {link}\n"
        f"📌 Source: {source}",
        disable_web_page_preview=True,
    )


async def setdefaultlink(update, context):
    if not await is_admin(update):
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /setdefaultlink https://t.me/channel"
        )
        return

    try:
        link = validate_telegram_link(context.args[0])
        set_setting("default_channel_link", link)

        await update.effective_message.reply_text(
            f"✅ Default anime link updated!\n{link}"
        )

    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def defaultlink(update, context):
    if not await is_admin(update):
        return

    link = (
        get_setting("default_channel_link")
        or DEFAULT_CHANNEL_LINK
    )

    await update.effective_message.reply_text(
        f"🔗 Default anime channel:\n{link}",
        disable_web_page_preview=True,
    )


# =========================================================
# ADMIN COMMANDS - POSTERS
# =========================================================

async def setposter(update, context):
    if not await is_admin(update):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/setposter Anime Name https://image-url"
        )
        return

    name = " ".join(context.args[:-1]).strip()

    try:
        url = validate_http_url(context.args[-1])

        posters_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name),
                "anime_name": name,
                "poster_url": url,
                "updated_at": time.time(),
            }},
            upsert=True,
        )

        await update.effective_message.reply_text(
            f"✅ Poster saved for {name}.\n{url}"
        )

        log_admin_action(
            update.effective_user.id,
            "setposter",
            name
        )

    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def removeposter(update, context):
    if not await is_admin(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.effective_message.reply_text(
            "Usage: /removeposter Anime Name"
        )
        return

    result = posters_col.delete_one({
        "anime_key": normalize_name(name)
    })

    await update.effective_message.reply_text(
        f"🗑️ Removed custom poster for {name}."
        if result.deleted_count
        else f"ℹ️ No custom poster found for {name}."
    )


async def listposters(update, context):
    if not await is_admin(update):
        return

    rows = list(
        posters_col.find({}).sort("anime_name", ASCENDING)
    )

    if not rows:
        await update.effective_message.reply_text(
            "No custom posters saved."
        )
        return

    lines = ["🖼️ CUSTOM POSTERS\n"]

    for row in rows:
        lines.append(
            f"🎬 {row['anime_name']}\n"
            f"🔗 {row['poster_url']}\n"
        )

    message = "\n".join(lines)

    for i in range(0, len(message), 3500):
        await update.effective_message.reply_text(
            message[i:i + 3500],
            disable_web_page_preview=True,
        )


async def setdefaultposter(update, context):
    if not await is_admin(update):
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/setdefaultposter https://image-url\n"
            "/setdefaultposter off"
        )
        return

    value = context.args[0]

    if value.casefold() == "off":
        set_setting("default_poster_url", "")
        await update.effective_message.reply_text(
            "✅ Default poster disabled."
        )
        return

    try:
        value = validate_http_url(value)
        set_setting("default_poster_url", value)

        await update.effective_message.reply_text(
            "✅ Default poster updated."
        )

    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


# =========================================================
# NEW COMMANDS - SEARCH AND INFORMATION
# =========================================================

async def searchanime(update, context):
    if not await is_admin(update):
        return

    query = " ".join(context.args).strip()

    if not query:
        await update.effective_message.reply_text(
            "Usage: /searchanime Anime Name"
        )
        return

    key = normalize_name(query)

    collections = [
        ("Route", routes_col, "channel_id"),
        ("Channel link", links_col, "channel_link"),
        ("Poster", posters_col, "poster_url"),
    ]

    results = []

    for label, collection, field in collections:
        docs = list(collection.find({}))

        for doc in docs:
            name = doc.get("anime_name", "")

            if key in normalize_name(name):
                results.append(
                    f"🎬 {name}\n"
                    f"📌 {label}: {doc.get(field)}"
                )

    if not results:
        await update.effective_message.reply_text(
            "No matching anime settings found."
        )
        return

    message = "🔎 SEARCH RESULTS\n\n" + "\n\n".join(results)

    for i in range(0, len(message), 3500):
        await update.effective_message.reply_text(
            message[i:i + 3500],
            disable_web_page_preview=True,
        )


async def animeinfo(update, context):
    if not await is_admin(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.effective_message.reply_text(
            "Usage: /animeinfo Anime Name"
        )
        return

    route = get_route(name)
    link = find_anime_document(links_col, name)
    poster = find_anime_document(posters_col, name)

    lines = [
        f"🎌 ANIME INFORMATION",
        f"🎬 Name: {name}",
        "",
        "📢 Upload route: "
        + (
            str(route.get("channel_id"))
            if route else "Not configured"
        ),
        "🔗 Custom channel link: "
        + (
            link.get("channel_link")
            if link else "Not configured"
        ),
        "🖼️ Custom poster: "
        + (
            poster.get("poster_url")
            if poster else "Not configured"
        ),
    ]

    await update.effective_message.reply_text(
        "\n".join(lines),
        disable_web_page_preview=True,
    )


# =========================================================
# STATISTICS
# =========================================================

async def stats_command(update, context):
    if not await is_admin(update):
        return

    route_count = routes_col.count_documents({})
    link_count = links_col.count_documents({})
    poster_count = posters_col.count_documents({})

    sent_count = history_col.count_documents({
        "status": "sent"
    })

    pending_count = history_col.count_documents({
        "status": "pending"
    })

    lines = [
        "📊 ANIME BOT STATISTICS",
        "",
        f"🧭 Anime routes: {route_count}",
        f"🔗 Custom anime links: {link_count}",
        f"🖼️ Custom posters: {poster_count}",
        f"📚 Recorded episodes: {sent_count}",
        f"⏳ Pending records: {pending_count}",
        "",
        f"📤 Announcement posts: {METRICS['episode_posts_ok']}",
        f"⚠️ Announcement failures: {METRICS['episode_posts_failed']}",
        f"📤 Upload posts: {METRICS['upload_posts_ok']}",
        f"⚠️ Upload failures: {METRICS['upload_posts_failed']}",
    ]

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


# =========================================================
# UPLOAD SOURCE PROCESSOR
# =========================================================

async def process_source_post(update, context):
    message = update.effective_message

    if not message or not SOURCE_CHANNEL_ID:
        return

    try:
        expected_id = int(SOURCE_CHANNEL_ID)
    except ValueError:
        logger.error("SOURCE_CHANNEL_ID must be numeric.")
        return

    if not update.effective_chat:
        return

    if update.effective_chat.id != expected_id:
        return

    post_text = get_message_text(message)
    parsed = parse_post(post_text)

    if not parsed:
        return

    anime_name = parsed["anime_name"]
    season = parsed["season_number"]
    episode = parsed["episode_number"]

    route = get_route(anime_name)

    if route:
        destination_id = int(route["channel_id"])
        destination_name = route["anime_name"]
    else:
        default_id = get_setting("default_channel_id")

        if not default_id:
            logger.warning(
                "No route/default channel for %s",
                anime_name
            )
            return

        destination_id = int(default_id)
        destination_name = "default channel"

    # Duplicate protection.
    episode_key = make_upload_key(
        anime_name,
        season,
        episode
    )

    reserved = reserve_episode(
        episode_key,
        {
            "type": "upload",
            "anime_name": anime_name,
            "season": season,
            "episode": episode,
            "source_chat_id": update.effective_chat.id,
            "source_message_id": message.message_id,
            "destination_id": destination_id,
        },
    )

    if not reserved:
        logger.info(
            "Duplicate upload ignored: %s S%s E%s",
            anime_name,
            season,
            episode,
        )
        return

    caption = make_caption(
        anime_name,
        season,
        episode
    )

    keyboard = make_keyboard(parsed["qualities"])
    image_url = parsed.get("image_url")

    try:
        sent_message = None

        if image_url:
            try:
                sent_message = await context.bot.send_photo(
                    chat_id=destination_id,
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception(
                    "Upload poster failed; sending text instead."
                )

        if not sent_message:
            sent_message = await context.bot.send_message(
                chat_id=destination_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        mark_episode_sent(
            episode_key,
            sent_message.message_id
        )

        METRICS["upload_posts_ok"] += 1

        logger.info(
            "Uploaded %s S%s E%s to %s",
            anime_name,
            season,
            episode,
            destination_name,
        )

    except Exception:
        release_episode(episode_key)

        METRICS["upload_posts_failed"] += 1

        logger.exception(
            "Failed upload publication for %s",
            anime_name,
        )


# =========================================================
# AUTO EPISODE ANNOUNCEMENTS
# =========================================================

def extract_episode_details(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    anime_name = "Unknown Anime"
    episode_number = "Unknown"
    season_number = "01"

    for line in lines:
        match = re.search(
            r"\bEpisode\s*[:#-]?\s*(\d+(?:\.\d+)?)",
            line,
            re.IGNORECASE,
        )

        if match:
            episode_number = match.group(1)

        match = re.search(
            r"\bSeason\s*[:#-]?\s*(\d+)",
            line,
            re.IGNORECASE,
        )

        if match:
            season_number = match.group(1)

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


async def handle_channel_post(update, context):
    source_message = update.channel_post

    if not source_message:
        return

    if not source_is_allowed(source_message.chat):
        return

    original_text = get_message_text(source_message)

    if not original_text or not contains_episode(original_text):
        return

    anime_name, episode, season = extract_episode_details(
        original_text
    )

    anime_link = get_anime_channel_link(anime_name)
    poster_url = get_anime_poster_url(anime_name)

    # Unique ID for this source post.
    episode_key = make_announcement_key(
        source_message.chat.id,
        source_message.message_id,
    )

    reserved = reserve_episode(
        episode_key,
        {
            "type": "announcement",
            "anime_name": anime_name,
            "season": season,
            "episode": episode,
            "source_chat_id": source_message.chat.id,
            "source_message_id": source_message.message_id,
            "destination_id": MAIN_CHANNEL_ID,
        },
    )

    if not reserved:
        logger.info(
            "Duplicate announcement ignored: %s",
            episode_key,
        )
        return

    announcement = (
        f"<blockquote><b>➪ {escape(anime_name)}</b></blockquote>\n"
        "──────────────────\n"
        f"<b>➥ Season : {escape(season)}\n"
        f"➤ Episode: {escape(episode)}\n"
        "➥ Quality: Multi Quality [Tamil Audio]</b>\n"
        "──────────────────\n"
        f"<blockquote><b>➽ NEW EPISODE {escape(episode)} ADDED 👇</b></blockquote>"
    )

    buttons = [
        [
            InlineKeyboardButton(
                "⎙• ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴊᴏɪɴ •⎙",
                url=anime_link,
            )
        ],
        [
            InlineKeyboardButton(
                "↪ ꜱʜᴀʀᴇ ᴄʜᴀɴɴᴇʟ",
                url="https://t.me/AnimeNetworkTamil",
            ),
            InlineKeyboardButton(
                "⚠️ ᴀɴʏ ɪꜱꜱᴜᴇꜱ ⚠️",
                url="https://t.me/Tamil_Animes_New",
            ),
        ],
    ]

    keyboard = InlineKeyboardMarkup(buttons)

    try:
        sent_message = None

        if poster_url:
            try:
                sent_message = await context.bot.send_photo(
                    chat_id=MAIN_CHANNEL_ID,
                    photo=poster_url,
                    caption=announcement,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception(
                    "Announcement poster failed; sending text."
                )

        if not sent_message:
            sent_message = await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=announcement,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        mark_episode_sent(
            episode_key,
            sent_message.message_id
        )

        METRICS["episode_posts_ok"] += 1

        logger.info(
            "Announcement sent for %s Episode %s",
            anime_name,
            episode,
        )

    except Exception:
        release_episode(episode_key)

        METRICS["episode_posts_failed"] += 1

        logger.exception(
            "Announcement failed for %s",
            anime_name,
        )


# =========================================================
# START / HELP / STATUS
# =========================================================

async def start_command(update, context):
    await update.effective_message.reply_text(
        "🔥 Anime Auto Publisher is running!\n\n"
        "Use /help to see commands.\n"
        "Use /status to check the bot."
    )


async def help_command(update, context):
    await update.effective_message.reply_text(
        "🎌 ANIME AUTO POST BOT\n\n"
        "📌 General:\n"
        "/start\n"
        "/help\n"
        "/status\n\n"
        "📢 Anime Routes:\n"
        "/addanime Anime Name ChannelID\n"
        "/removeanime Anime Name\n"
        "/listanime\n"
        "/setdefault ChannelID\n"
        "/defaultchannel\n\n"
        "🔗 Anime Links:\n"
        "/setanimelink Anime Name Link\n"
        "/removeanimelink Anime Name\n"
        "/listanimelinks\n"
        "/getanimelink Anime Name\n"
        "/setdefaultlink Link\n"
        "/defaultlink\n\n"
        "🖼️ Posters:\n"
        "/setposter Anime Name URL\n"
        "/removeposter Anime Name\n"
        "/listposters\n"
        "/setdefaultposter URL or off\n\n"
        "🛠️ Tools:\n"
        "/stats\n"
        "/searchanime Anime Name\n"
        "/animeinfo Anime Name"
    )


async def status_command(update, context):
    if not update.effective_message:
        return

    try:
        mongo_client.admin.command("ping")

        route_count = routes_col.count_documents({})
        link_count = links_col.count_documents({})
        poster_count = posters_col.count_documents({})

        db_state = "✅ Connected"

    except Exception as exc:
        logger.exception("MongoDB status check failed")

        route_count = 0
        link_count = 0
        poster_count = 0

        db_state = f"❌ Unavailable ({type(exc).__name__})"

    uptime = max(0, int(time.time() - STARTED_AT))

    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)

    lines = [
        "🤖 COMBINED ANIME BOT STATUS",
        "🟢 Process: Running",
        f"⏱️ Uptime: {hours}h {minutes}m {seconds}s",
        f"🗄️ MongoDB: {db_state}",
        f"📢 Announcement sources: {len(SOURCE_CHANNEL_IDS)}",
        f"🎬 Upload source: {'Configured' if SOURCE_CHANNEL_ID else 'Not configured'}",
        f"🧭 Anime routes: {route_count}",
        f"🔗 Anime links: {link_count}",
        f"🖼️ Custom posters: {poster_count}",
        f"📤 Announcement posts: {METRICS['episode_posts_ok']}",
        f"⚠️ Announcement failures: {METRICS['episode_posts_failed']}",
        f"📤 Upload posts: {METRICS['upload_posts_ok']}",
        f"⚠️ Upload failures: {METRICS['upload_posts_failed']}",
    ]

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    error = context.error

    if error:
        logger.error(
            "Unhandled Telegram error",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )


# =========================================================
# MAIN
# =========================================================

def main():
    # Connect to MongoDB.
    init_db()

    # Import old SQLite records if available.
    migrate_sqlite()

    # Start Render health-check server.
    Thread(
        target=start_web_server,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # General commands.
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    # Anime route commands.
    application.add_handler(
        CommandHandler("addanime", addanime)
    )

    application.add_handler(
        CommandHandler("removeanime", removeanime)
    )

    application.add_handler(
        CommandHandler("listanime", listanime)
    )

    application.add_handler(
        CommandHandler("setdefault", setdefault)
    )

    application.add_handler(
        CommandHandler("defaultchannel", defaultchannel)
    )

    # Anime link commands.
    application.add_handler(
        CommandHandler("setanimelink", setanimelink)
    )

    application.add_handler(
        CommandHandler("removeanimelink", removeanimelink)
    )

    application.add_handler(
        CommandHandler("listanimelinks", listanimelinks)
    )

    application.add_handler(
        CommandHandler("getanimelink", getanimelink)
    )

    application.add_handler(
        CommandHandler("setdefaultlink", setdefaultlink)
    )

    application.add_handler(
        CommandHandler("defaultlink", defaultlink)
    )

    # Poster commands.
    application.add_handler(
        CommandHandler("setposter", setposter)
    )

    application.add_handler(
        CommandHandler("removeposter", removeposter)
    )

    application.add_handler(
        CommandHandler("listposters", listposters)
    )

    application.add_handler(
        CommandHandler("setdefaultposter", setdefaultposter)
    )

    # New tools.
    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CommandHandler("searchanime", searchanime)
    )

    application.add_handler(
        CommandHandler("animeinfo", animeinfo)
    )

    # Channel post handlers.
    channel_filter = (
        filters.ChatType.CHANNEL
        & (filters.TEXT | filters.CAPTION)
    )

    application.add_handler(
        MessageHandler(
            channel_filter,
            handle_channel_post,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            channel_filter,
            process_source_post,
        ),
        group=1,
    )

    application.add_error_handler(error_handler)

    logger.info("Starting MongoDB Anime Auto Post Bot.")

    application.run_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
