import os
import re
import io
import time
import html
import json
import logging
import sqlite3
import secrets
from pathlib import Path
from threading import Thread
from contextlib import closing
from urllib.parse import urlparse
from datetime import datetime, timezone

import requests
from bson import json_util
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from flask import Flask, request, redirect, session, send_file, render_template_string
from PIL import Image, ImageDraw, ImageFont, ImageOps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# =========================================================
# CONFIGURATION
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("anime_auto_post_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "").strip()
SOURCE_CHANNEL_IDS = [
    x.strip() for x in os.getenv("SOURCE_CHANNEL_IDS", "").split(",") if x.strip()
]
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "anime_auto_post_bot").strip()
PORT = int(os.getenv("PORT", "10000"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "/var/data/anime_channels.sqlite3").strip()
MIGRATE_SQLITE = os.getenv("MIGRATE_SQLITE", "true").lower() in ("1", "true", "yes")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "").strip() or secrets.token_hex(32)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing in Render environment variables.")
if not MAIN_CHANNEL_ID:
    raise ValueError("MAIN_CHANNEL_ID is missing in Render environment variables.")
if not SOURCE_CHANNEL_IDS:
    raise ValueError("SOURCE_CHANNEL_IDS is missing in Render environment variables.")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI is missing in Render environment variables.")

STARTED_AT = time.time()
METRICS = {
    "announcement_ok": 0, "announcement_failed": 0,
    "upload_ok": 0, "upload_failed": 0,
}

# Every Telegram send operation uses these longer timeout values.
TG_TIMEOUTS = {
    "read_timeout": 60,
    "write_timeout": 60,
    "connect_timeout": 30,
    "pool_timeout": 30,
}

# =========================================================
# BUILT-IN ANIME LINKS AND POSTERS
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
    "demon slayer": "https://t.me/DemonSlayerChannel",
}
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
}
DEFAULT_CHANNEL_LINK = "https://t.me/AnimeNetworkTamil"

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
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def init_db():
    mongo_client.admin.command("ping")
    for collection in (routes_col, posters_col, links_col):
        collection.create_index([("anime_key", ASCENDING)], unique=True)
    history_col.create_index([("created_at", DESCENDING)])
    history_col.create_index([("status", ASCENDING)])
    logs_col.create_index([("created_at", DESCENDING)])
    logger.info("MongoDB connected and indexes ready.")


def migrate_sqlite():
    if not MIGRATE_SQLITE or not os.path.isfile(DATABASE_PATH):
        logger.info("SQLite migration skipped.")
        return
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            mappings = [
                ("anime_routes", "SELECT anime_key, anime_name, channel_id FROM anime_routes",
                 routes_col, ("anime_key", "anime_name", "channel_id")),
                ("anime_posters", "SELECT anime_key, anime_name, poster_url FROM anime_posters",
                 posters_col, ("anime_key", "anime_name", "poster_url")),
                ("anime_links", "SELECT anime_key, anime_name, channel_link FROM anime_links",
                 links_col, ("anime_key", "anime_name", "channel_link")),
            ]
            for table, query, collection, fields in mappings:
                if table in tables:
                    for row in conn.execute(query):
                        doc = {field: row[field] for field in fields}
                        if table == "anime_routes":
                            doc["channel_id"] = int(doc["channel_id"])
                        collection.update_one(
                            {"anime_key": doc["anime_key"]}, {"$set": doc}, upsert=True
                        )
            if "settings" in tables:
                for row in conn.execute("SELECT key, value FROM settings"):
                    settings_col.update_one(
                        {"_id": row["key"]},
                        {"$set": {"value": str(row["value"])}}, upsert=True
                    )
        logger.info("SQLite migration completed.")
    except Exception:
        logger.exception("SQLite migration failed.")
        raise


def get_setting(key, default=None):
    doc = settings_col.find_one({"_id": key})
    return doc.get("value", default) if doc else default


def set_setting(key, value):
    settings_col.update_one(
        {"_id": key}, {"$set": {"value": str(value)}}, upsert=True
    )


def log_admin_action(user_id, action, details=""):
    try:
        logs_col.insert_one({
            "user_id": user_id, "action": action,
            "details": str(details)[:1000], "created_at": time.time(),
        })
    except Exception:
        logger.exception("Admin log write failed.")


def find_anime_document(collection, anime_name):
    key = normalize_name(anime_name)
    exact = collection.find_one({"anime_key": key})
    if exact:
        return exact
    matches = []
    for doc in collection.find({}, limit=5000):
        title = normalize_name(doc.get("anime_name", ""))
        if title and (title in key or key in title):
            matches.append(doc)
    return sorted(
        matches,
        key=lambda item: len(normalize_name(item.get("anime_name", ""))),
        reverse=True,
    )[0] if matches else None


def get_matching_value(mapping, anime_name):
    key = normalize_name(anime_name)
    if key in mapping:
        return mapping[key]
    matches = [
        (normalize_name(title), value)
        for title, value in mapping.items()
        if normalize_name(title) in key or key in normalize_name(title)
    ]
    return sorted(matches, key=lambda item: len(item[0]), reverse=True)[0][1] if matches else None


def get_route(anime_name):
    return find_anime_document(routes_col, anime_name)


def get_anime_channel_link(anime_name):
    doc = find_anime_document(links_col, anime_name)
    return (
        (doc.get("channel_link") if doc else None)
        or get_matching_value(ANIME_CHANNEL_LINKS, anime_name)
        or get_setting("default_channel_link")
        or DEFAULT_CHANNEL_LINK
    )


def get_anime_poster_url(anime_name):
    doc = find_anime_document(posters_col, anime_name)
    return (
        (doc.get("poster_url") if doc else None)
        or get_matching_value(ANIME_POSTERS, anime_name)
        or get_setting("default_poster_url")
        or None
    )


# =========================================================
# VALIDATION AND ACCESS
# =========================================================

def parse_channel_id(value):
    value = str(value).strip()
    if not re.fullmatch(r"-?\d+", value):
        raise ValueError("Enter a numeric Telegram channel ID, e.g. -1001234567890.")
    return int(value)


def validate_http_url(value):
    value = str(value).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Please provide a valid HTTP/HTTPS URL.")
    return value


def validate_telegram_link(value):
    value = validate_http_url(value)
    if urlparse(value).hostname not in (
        "t.me", "www.t.me", "telegram.me", "www.telegram.me"
    ):
        raise ValueError("Please provide a Telegram channel link.")
    return value


def normalize_channel_id(value):
    value = str(value).strip()
    try:
        return str(int(value))
    except ValueError:
        return value.lstrip("@").lower()


def source_is_allowed(chat):
    configured = {normalize_channel_id(value) for value in SOURCE_CHANNEL_IDS}
    if str(chat.id) in configured:
        return True
    username = getattr(chat, "username", None)
    return bool(username and normalize_channel_id(username) in configured)


async def is_admin(update):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False
    if not ADMIN_IDS:
        await message.reply_text("⚠️ ADMIN_IDS is not configured in Render.")
        return False
    if user.id not in ADMIN_IDS:
        await message.reply_text("⛔ This command is admin-only.")
        return False
    return True


# =========================================================
# POSTER GENERATOR (TELEGRAM UPLOAD ONLY - NO PUBLIC URL)
# ========================================================


# =========================================================
# SOURCE MESSAGE PARSER AND CAPTIONS
# =========================================================

def get_message_text(message):
    return message.text or message.caption or ""


def parse_post(text):
    if not text:
        return None

    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(raw_lines) < 2:
        return None

    image_re = re.compile(
        r"^\s*(?:image(?:\s*url)?|poster|thumbnail|photo)\s*[:=\-]?\s*(https?://\S+)\s*$",
        re.I,
    )
    image_url, lines = None, []
    for line in raw_lines:
        match = image_re.match(line)
        if match:
            try:
                image_url = validate_http_url(match.group(1).rstrip(".,;)"))
            except ValueError:
                pass
        else:
            lines.append(line)

    season_re = re.compile(r"\b(?:season|s)\s*[-:#]?\s*(\d+)\b", re.I)
    season = "1"
    for line in lines:
        match = season_re.search(line)
        if match:
            season = match.group(1)
            break

    episode_re = re.compile(
        r"\b(?:(?:season|s)\s*\d+\s*[-:]?\s*)?(?:episode|ep|e)\s*[-:#]?\s*(\d+(?:\.\d+)?)\b",
        re.I,
    )
    episode_index, episode = None, None
    for index, line in enumerate(lines):
        match = episode_re.search(line)
        if match:
            episode_index, episode = index, match.group(1)
            break
    if episode_index is None:
        return None

    anime = " ".join(lines[:episode_index])
    anime = season_re.sub("", anime).strip(" \t-–—:|")
    anime = re.sub(r"^(?:anime|title)\s*[:=-]\s*", "", anime, flags=re.I).strip()
    if not anime:
        return None

    quality_re = re.compile(
        r"\b(144|240|360|480|540|576|720|1080|1440|2160)\s*[pP]\b"
        r"\s*[-:–—]?\s*(https?://[^\s|<>]+)", re.I,
    )
    qualities = {}
    for line in lines[episode_index:]:
        for match in quality_re.finditer(line):
            quality = f"{match.group(1)}P"
            try:
                qualities[quality] = validate_http_url(match.group(2).rstrip(".,;)"))
            except ValueError:
                pass

    if not qualities:
        return None

    return {
        "anime_name": anime,
        "season_number": season,
        "episode_number": episode,
        "qualities": qualities,
        "image_url": image_url,
    }


def make_caption(anime, season, episode):
    template = get_setting("upload_caption_template") or (
        "<b>{anime}</b> | தமிழ் Official Dub\n\n"
        "Season : {season} | Episode : {episode}\n\n"
        "‼️ Note - Click The Below Button to Get Episodes 👇"
    )
    values = {
        "anime": html.escape(str(anime)),
        "season": html.escape(str(season).zfill(2)),
        "episode": html.escape(str(episode)),
    }
    try:
        return template.format(**values)[:1000]
    except Exception:
        return (
            f"<b>{values['anime']}</b> | தமிழ் Official Dub\n\n"
            f"Season : {values['season']} | Episode : {values['episode']}\n\n"
            "‼️ Click below to get episodes 👇"
        )


def make_quality_keyboard(qualities):
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
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def make_announcement_keyboard(anime):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⎙• ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴊᴏɪɴ •⎙",
                url=get_anime_channel_link(anime),
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
    ])


# =========================================================
# DUPLICATE PROTECTION AND HISTORY
# =========================================================

PENDING_LEASE_SECONDS = 15 * 60


def reserve_episode(key, data):
    now = time.time()
    document = {"_id": key, **data, "status": "pending", "created_at": now}
    try:
        history_col.insert_one(document)
        return True
    except DuplicateKeyError:
        existing = history_col.find_one({"_id": key})
        if not existing or existing.get("status") == "sent":
            return False
        if now - float(existing.get("created_at", now)) < PENDING_LEASE_SECONDS:
            return False
        result = history_col.update_one(
            {"_id": key, "status": "pending", "created_at": existing.get("created_at")},
            {"$set": {
                "created_at": now,
                "retry_count": int(existing.get("retry_count", 0)) + 1,
            }},
        )
        return result.modified_count == 1
    except PyMongoError:
        logger.exception("MongoDB episode reservation failed.")
        return False


def mark_episode_sent(key, message_id):
    history_col.update_one(
        {"_id": key},
        {"$set": {
            "status": "sent",
            "telegram_message_id": message_id,
            "sent_at": time.time(),
        }},
    )


def release_episode(key):
    history_col.delete_one({"_id": key, "status": "pending"})


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def start_command(update, context):
    await update.effective_message.reply_text(
        "🔥 Bot Ku Uyir Iruku!\nUse /help to see commands."
    )


async def help_command(update, context):
    await update.effective_message.reply_text(
        "🎌 ANIME AUTO POST BOT \n\n"
        "General: /start /help /status\n"
        "Routes: /addanime Name ChannelID | /removeanime Name | /listanime\n"
        "Default: /setdefault ChannelID | /defaultchannel\n"
        "Links: /setanimelink Name Link | /removeanimelink Name | /listanimelinks\n"
        "/getanimelink Name | /setdefaultlink Link | /defaultlink\n"
        "Posters: /setposter Name URL | /removeposter Name | /listposters\n"
        "/setdefaultposter URL_or_off\n"
        "Tools: /stats | /searchanime Name | /animeinfo Name\n"
        "History: /history [Anime Name] | /clearhistory Anime Name\n"
        "Backup: /backup | reply to backup JSON with /restore\n"
        "Caption: /setcaption HTML_template | /defaultcaption\n"
        "Dashboard: /dashboard"
    )


async def status_command(update, context):
    try:
        mongo_client.admin.command("ping")
        db_status = "Connected"
    except Exception as exc:
        db_status = f"Unavailable ({type(exc).__name__})"

    uptime = max(0, int(time.time() - STARTED_AT))
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)

    await update.effective_message.reply_text(
        "🤖 ANIME AUTO POST BOT V3\n"
        f"🟢 Process: Running\n⏱️ Uptime: {hours}h {minutes}m {seconds}s\n"
        f"🗄️ MongoDB: {db_status}\n"
        f"📢 Announcement sources: {len(SOURCE_CHANNEL_IDS)}\n"
        f"🎬 Upload source: {'Configured' if SOURCE_CHANNEL_ID else 'Not configured'}\n"
        f"📤 Announcements: {METRICS['announcement_ok']} ok / {METRICS['announcement_failed']} failed\n"
        f"📤 Uploads: {METRICS['upload_ok']} ok / {METRICS['upload_failed']} failed"
    )


async def addanime(update, context):
    if not await is_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /addanime Anime Name -1001234567890")
        return
    try:
        channel_id = parse_channel_id(context.args[-1])
        name = " ".join(context.args[:-1]).strip()
        chat = await context.bot.get_chat(channel_id)
        routes_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name), "anime_name": name,
                "channel_id": channel_id, "updated_at": time.time(),
            }},
            upsert=True,
        )
        log_admin_action(update.effective_user.id, "addanime", name)
        await update.effective_message.reply_text(
            f"✅ Route saved\n🎬 {name}\n📢 {chat.title or channel_id}\n🆔 {channel_id}"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Could not save route: {exc}")


async def removeanime(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeanime Anime Name")
        return
    result = routes_col.delete_one({"anime_key": normalize_name(name)})
    await update.effective_message.reply_text(
        "🗑️ Route removed." if result.deleted_count else "ℹ️ Route not found."
    )


async def listanime(update, context):
    if not await is_admin(update):
        return
    rows = list(routes_col.find({}).sort("anime_name", ASCENDING).limit(100))
    if not rows:
        await update.effective_message.reply_text("No anime routes registered.")
        return
    message = "📚 REGISTERED ROUTES\n\n" + "\n\n".join(
        f"🎬 {row.get('anime_name')}\n🆔 {row.get('channel_id')}" for row in rows
    )
    for index in range(0, len(message), 3500):
        await update.effective_message.reply_text(message[index:index + 3500])


async def setdefault(update, context):
    if not await is_admin(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefault -1001234567890")
        return
    try:
        channel_id = parse_channel_id(context.args[0])
        chat = await context.bot.get_chat(channel_id)
        set_setting("default_channel_id", channel_id)
        await update.effective_message.reply_text(
            f"✅ Default channel set: {chat.title or channel_id}\n{channel_id}"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def defaultchannel(update, context):
    if not await is_admin(update):
        return
    value = get_setting("default_channel_id")
    await update.effective_message.reply_text(
        f"📢 Default channel ID: {value}" if value else "No default channel. Use /setdefault."
    )


async def setanimelink(update, context):
    if not await is_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setanimelink Anime Name https://t.me/channel"
        )
        return
    name = " ".join(context.args[:-1]).strip()
    try:
        link = validate_telegram_link(context.args[-1])
        links_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name), "anime_name": name,
                "channel_link": link, "updated_at": time.time(),
            }},
            upsert=True,
        )
        await update.effective_message.reply_text(f"✅ Link saved for {name}\n{link}")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def removeanimelink(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeanimelink Anime Name")
        return
    result = links_col.delete_one({"anime_key": normalize_name(name)})
    await update.effective_message.reply_text(
        "🗑️ Link removed." if result.deleted_count else "ℹ️ Link not found."
    )


async def listanimelinks(update, context):
    if not await is_admin(update):
        return
    rows = list(links_col.find({}).sort("anime_name", ASCENDING).limit(80))
    if not rows:
        await update.effective_message.reply_text("No custom anime links saved.")
        return
    message = "🎌 SAVED LINKS\n\n" + "\n\n".join(
        f"{row.get('anime_name')}\n{row.get('channel_link')}" for row in rows
    )
    for index in range(0, len(message), 3500):
        await update.effective_message.reply_text(
            message[index:index + 3500], disable_web_page_preview=True
        )


async def getanimelink(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /getanimelink Anime Name")
        return
    await update.effective_message.reply_text(
        f"🎬 {name}\n🔗 {get_anime_channel_link(name)}",
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
        set_setting("default_channel_link", validate_telegram_link(context.args[0]))
        await update.effective_message.reply_text("✅ Default channel link updated.")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def defaultlink(update, context):
    if not await is_admin(update):
        return
    await update.effective_message.reply_text(
        get_setting("default_channel_link") or DEFAULT_CHANNEL_LINK,
        disable_web_page_preview=True,
    )


async def setposter(update, context):
    if not await is_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /setposter Anime Name https://image-url")
        return
    name = " ".join(context.args[:-1]).strip()
    try:
        poster_url = validate_http_url(context.args[-1])
        posters_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name), "anime_name": name,
                "poster_url": poster_url, "updated_at": time.time(),
            }},
            upsert=True,
        )
        await update.effective_message.reply_text(f"✅ Poster saved for {name}\n{poster_url}")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def removeposter(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeposter Anime Name")
        return
    result = posters_col.delete_one({"anime_key": normalize_name(name)})
    await update.effective_message.reply_text(
        "🗑️ Poster removed." if result.deleted_count else "ℹ️ Poster not found."
    )


async def listposters(update, context):
    if not await is_admin(update):
        return
    rows = list(posters_col.find({}).sort("anime_name", ASCENDING).limit(80))
    if not rows:
        await update.effective_message.reply_text("No custom posters saved.")
        return
    message = "🖼️ CUSTOM POSTERS\n\n" + "\n\n".join(
        f"{row.get('anime_name')}\n{row.get('poster_url')}" for row in rows
    )
    for index in range(0, len(message), 3500):
        await update.effective_message.reply_text(
            message[index:index + 3500], disable_web_page_preview=True
        )


async def setdefaultposter(update, context):
    if not await is_admin(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefaultposter URL_or_off")
        return
    if context.args[0].lower() == "off":
        set_setting("default_poster_url", "")
        await update.effective_message.reply_text("✅ Default poster disabled.")
        return
    try:
        set_setting("default_poster_url", validate_http_url(context.args[0]))
        await update.effective_message.reply_text("✅ Default poster updated.")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def searchanime(update, context):
    if not await is_admin(update):
        return
    query = normalize_name(" ".join(context.args))
    if not query:
        await update.effective_message.reply_text("Usage: /searchanime Anime Name")
        return
    results = []
    for label, collection, field in (
        ("Route", routes_col, "channel_id"),
        ("Link", links_col, "channel_link"),
        ("Poster", posters_col, "poster_url"),
    ):
        for doc in collection.find({}, limit=5000):
            if query in normalize_name(doc.get("anime_name", "")):
                results.append(
                    f"🎬 {doc.get('anime_name')}\n{label}: {doc.get(field)}"
                )
    result_text = (
        "🔎 RESULTS\n\n" + "\n\n".join(results)
        if results else "No matching anime settings."
    )
    await update.effective_message.reply_text(
        result_text[:3900], disable_web_page_preview=True
    )


async def animeinfo(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /animeinfo Anime Name")
        return
    route = get_route(name)
    link = find_anime_document(links_col, name)
    poster = find_anime_document(posters_col, name)
    await update.effective_message.reply_text(
        f"🎌 ANIME INFORMATION\n🎬 {name}\n\n"
        f"📢 Route: {route.get('channel_id') if route else 'Not configured'}\n"
        f"🔗 Link: {link.get('channel_link') if link else get_anime_channel_link(name)}\n"
        f"🖼️ Poster: {poster.get('poster_url') if poster else get_anime_poster_url(name) or 'Not configured'}",
        disable_web_page_preview=True,
    )


async def stats_command(update, context):
    if not await is_admin(update):
        return
    await update.effective_message.reply_text(
        "📊 BOT STATISTICS\n\n"
        f"Routes: {routes_col.count_documents({})}\n"
        f"Custom links: {links_col.count_documents({})}\n"
        f"Custom posters: {posters_col.count_documents({})}\n"
        f"Sent history: {history_col.count_documents({'status': 'sent'})}\n"
        f"Pending history: {history_col.count_documents({'status': 'pending'})}\n"
        f"Uploads: {METRICS['upload_ok']} / failed {METRICS['upload_failed']}\n"
        f"Announcements: {METRICS['announcement_ok']} / failed {METRICS['announcement_failed']}"
    )


async def history_command(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    query = {"status": "sent"}
    if name:
        query["anime_name"] = {"$regex": re.escape(name), "$options": "i"}
    rows = list(history_col.find(query).sort("sent_at", DESCENDING).limit(15))
    if not rows:
        await update.effective_message.reply_text("No matching sent episode history.")
        return
    lines = ["📚 RECENT EPISODE HISTORY\n"]
    for row in rows:
        timestamp = row.get("sent_at", row.get("created_at", time.time()))
        date_text = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"🎬 {row.get('anime_name', 'Unknown')} | "
            f"S{row.get('season', '?')} E{row.get('episode', '?')}\n"
            f"📌 {row.get('type', '?')} | {date_text}"
        )
    await update.effective_message.reply_text("\n\n".join(lines))


async def clearhistory(update, context):
    if not await is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /clearhistory Anime Name")
        return
    result = history_col.delete_many({
        "anime_name": {"$regex": re.escape(name), "$options": "i"}
    })
    await update.effective_message.reply_text(
        f"🧹 Removed {result.deleted_count} history records for {name}."
    )


async def setcaption(update, context):
    if not await is_admin(update):
        return
    template = " ".join(context.args).strip()
    if not template:
        await update.effective_message.reply_text(
            "Usage: /setcaption <b>{anime}</b> Season {season} Episode {episode}\n"
            "Placeholders: {anime}, {season}, {episode}"
        )
        return
    try:
        template.format(anime="Anime", season="01", episode="01")
    except Exception:
        await update.effective_message.reply_text(
            "❌ Invalid template. Use {anime}, {season}, and {episode}."
        )
        return
    set_setting("upload_caption_template", template)
    await update.effective_message.reply_text("✅ Upload caption template saved.")


async def defaultcaption(update, context):
    if not await is_admin(update):
        return
    set_setting("upload_caption_template", "")
    await update.effective_message.reply_text("✅ Default upload caption restored.")


async def backup_command(update, context):
    if not await is_admin(update):
        return
    names = [
        "anime_routes", "bot_settings", "anime_posters",
        "anime_links", "episode_history", "admin_logs",
    ]
    payload = {
        "backup_version": 3,
        "created_at": time.time(),
        "database": MONGODB_DATABASE,
        "collections": {},
    }
    for name in names:
        payload["collections"][name] = list(db[name].find({}))
    document = io.BytesIO(json_util.dumps(payload, indent=2).encode("utf-8"))
    document.name = f"anime_bot_backup_{int(time.time())}.json"
    await update.effective_message.reply_document(
        document=document,
        caption="🗄️ MongoDB backup. Keep this file private.",
        **TG_TIMEOUTS,
    )


async def restore_command(update, context):
    if not await is_admin(update):
        return
    message = update.effective_message
    document = (
        message.reply_to_message.document
        if message.reply_to_message else None
    )
    if not document:
        await message.reply_text("Reply to a bot backup JSON file with /restore.")
        return
    try:
        telegram_file = await context.bot.get_file(document.file_id, **TG_TIMEOUTS)
        content = bytes(await telegram_file.download_as_bytearray())
        payload = json_util.loads(content.decode("utf-8"))
        allowed = {
            "anime_routes", "bot_settings", "anime_posters",
            "anime_links", "episode_history", "admin_logs",
        }
        restored = 0
        for name, documents in payload.get("collections", {}).items():
            if name not in allowed or not isinstance(documents, list):
                continue
            for item in documents:
                if isinstance(item, dict) and "_id" in item:
                    db[name].replace_one({"_id": item["_id"]}, item, upsert=True)
                    restored += 1
        await message.reply_text(
            f"✅ Restore merge completed. Records processed: {restored}\n"
            "Existing records not in the backup were kept."
        )
    except Exception as exc:
        logger.exception("Restore failed.")
        await message.reply_text(f"❌ Restore failed: {type(exc).__name__}")


async def dashboard_command(update, context):
    if not await is_admin(update):
        return
    await update.effective_message.reply_text(
        "🖥️ Dashboard: open your Render service URL + /dashboard\n"
        "Sign in using DASHBOARD_PASSWORD."
    )


# =========================================================
# CHANNEL POST PROCESSING
# =========================================================

def contains_episode(text):
    return bool(re.search(r"\b(?:episode|ep)\b", text or "", re.I))



async def process_source_post(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not SOURCE_CHANNEL_ID:
        return

    try:
        expected_id = int(SOURCE_CHANNEL_ID)
    except ValueError:
        logger.error("SOURCE_CHANNEL_ID must be a numeric channel ID.")
        return

    if chat.id != expected_id:
        return

    parsed = parse_post(get_message_text(message))
    if not parsed:
        return

    anime = parsed["anime_name"]
    season = parsed["season_number"]
    episode = parsed["episode_number"]

    # Always publish to Main Channel.
    
    # Also publish to the assigned anime channel.
    route = get_route(anime)

    if route:
        anime_destination = str(route["channel_id"])
    else:
        # Use default channel if no anime-specific route exists.
        default_id = get_setting("default_channel_id")
        anime_destination = str(default_id) if default_id else None

    if anime_destination and anime_destination not in destinations:
        destinations.append(anime_destination)

    key = f"upload:{normalize_name(anime)}:s{season}:e{episode}"

    if not reserve_episode(key, {
        "type": "upload",
        "anime_name": anime,
        "season": season,
        "episode": episode,
        "source_chat_id": chat.id,
        "source_message_id": message.message_id,
        "destination_ids": destinations,
    }):
        logger.info("Duplicate or active upload ignored: %s", key)
        return

    # Keep track of successful destinations to avoid reposting
    # to a channel if a retry happens after partial success.
    existing = history_col.find_one({"_id": key}) or {}
    sent_destinations = set(
        str(item) for item in existing.get("sent_destinations", [])
    )

    background = (
        parsed.get("image_url")
        or get_anime_poster_url(anime)
    )

    caption = make_caption(anime, season, episode)
    keyboard = make_quality_keyboard(parsed["qualities"])

    try:
        for destination_id in destinations:
            destination_key = str(destination_id)

            if destination_key in sent_destinations:
                continue

            sent = None

            # Send poster with caption when an image URL exists.
            if background:
                try:
                    sent = await context.bot.send_photo(
                        chat_id=destination_id,
                        photo=background,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                        **TG_TIMEOUTS,
                    )
                except Exception:
                    logger.exception(
                        "Poster send failed for %s. Trying text fallback.",
                        destination_id,
                    )

            # Fallback if no poster exists or photo sending fails.
            if sent is None:
                sent = await context.bot.send_message(
                    chat_id=destination_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                    **TG_TIMEOUTS,
                )

            # Save successful destination immediately.
            history_col.update_one(
                {"_id": key},
                {"$addToSet": {"sent_destinations": destination_key}},
            )

            sent_destinations.add(destination_key)

            logger.info(
                "Episode published: %s S%s E%s -> %s",
                anime, season, episode, destination_id,
            )

        # Mark complete only after every destination succeeds.
        mark_episode_sent(key, sent.message_id)

        METRICS["upload_ok"] += 1

        logger.info(
            "Episode successfully published to all destinations: %s",
            anime,
        )

    except Exception:
        # Keep pending history for safe retry after the lease expires.
        # Previously successful destinations are recorded above.
        METRICS["upload_failed"] += 1

        logger.exception(
            "Episode publication incomplete for %s. Successful destinations: %s",
            anime,
            list(sent_destinations),
        )


def extract_episode_details(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    anime, episode, season = "Unknown Anime", "Unknown", "01"

    for line in lines:
        match = re.search(r"\bEpisode\s*[:#-]?\s*(\d+(?:\.\d+)?)", line, re.I)
        if match:
            episode = match.group(1)
        match = re.search(r"\bSeason\s*[:#-]?\s*(\d+)", line, re.I)
        if match:
            season = match.group(1).zfill(2)

    for index, line in enumerate(lines):
        if re.search(r"\bEpisode\b", line, re.I):
            if index > 0:
                candidate = lines[index - 1]
                if not re.search(
                    r"\b(480p|720p|1080p|2160p|Tamil|English|Japanese|Season)\b",
                    candidate, re.I,
                ):
                    anime = candidate.strip("➪➥➤-:| ")
            break

    if anime == "Unknown Anime":
        for line in lines:
            if re.search(
                r"\b(Episode|Season|480p|720p|1080p|2160p|Tamil|English|Japanese)\b",
                line, re.I,
            ):
                continue
            anime = line.strip("➪➥➤-:| ")
            break
    return anime, episode, season


async def handle_channel_post(update, context):
    source = update.channel_post
    if not source or not source_is_allowed(source.chat):
        return

    text = get_message_text(source)
    if not text or not contains_episode(text):
        return

    anime, episode, season = extract_episode_details(text)
    key = f"announcement:{source.chat.id}:{source.message_id}"

    if not reserve_episode(key, {
        "type": "announcement",
        "anime_name": anime,
        "season": season,
        "episode": episode,
        "source_chat_id": source.chat.id,
        "source_message_id": source.message_id,
        "destination_id": MAIN_CHANNEL_ID,
    }):
        return

    announcement = (
        f"<blockquote><b>➪ {html.escape(anime)}</b></blockquote>\n"
        "──────────────────\n"
        f"<b>➥ Season : {html.escape(season)}\n"
        f"➤ Episode: {html.escape(episode)}\n"
        "➥ Quality: Multi Quality [Tamil Audio]</b>\n"
        "──────────────────\n"
        f"<blockquote><b>➽ NEW EPISODE {html.escape(episode)} ADDED 👇</b></blockquote>"
    )
    keyboard = make_announcement_keyboard(anime)
    poster_url = get_anime_poster_url(anime)

    try:
        sent = None
        if poster_url:
            try:
                sent = await context.bot.send_photo(
                    chat_id=MAIN_CHANNEL_ID,
                    photo=poster_url,
                    caption=announcement,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    **TG_TIMEOUTS,
                )
            except Exception:
                logger.exception("Announcement poster send failed; trying text fallback.")

        if sent is None:
            sent = await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=announcement,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
                **TG_TIMEOUTS,
            )

        mark_episode_sent(key, sent.message_id)
        METRICS["announcement_ok"] += 1
        logger.info("Announcement sent for %s episode %s", anime, episode)
    except Exception:
        release_episode(key)
        METRICS["announcement_failed"] += 1
        logger.exception("Announcement failed for %s", anime)


# =========================================================
# FLASK DASHBOARD (NO PUBLIC POSTER URL ROUTE)
# =========================================================

web = Flask(__name__)
web.secret_key = DASHBOARD_SECRET
web.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)

STYLE = """
<style>
body{font-family:Arial;background:#101827;color:#edf2f7;margin:0;padding:22px}
.wrap{max-width:1000px;margin:auto}.card{background:#1c293b;padding:18px;border-radius:14px;margin:14px 0}
input,button{padding:11px;border-radius:8px;border:0;margin:5px 0;max-width:100%}
input{background:#edf2f7;color:#111;width:95%}button{background:#f2b84b;color:#111;font-weight:bold}
a{color:#ffd27a}table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #435064;text-align:left}
</style>
"""
LOGIN_PAGE = """<!doctype html><html><head><title>Anime Bot Login</title>""" + STYLE + """
</head><body><div class="wrap"><div class="card"><h1>🎌 Anime Bot Dashboard</h1>
<form method="post"><input type="password" name="password" placeholder="Dashboard password" required>
<button>Sign in</button></form></div></div></body></html>"""
DASHBOARD_PAGE = """<!doctype html><html><head><title>Anime Bot Dashboard</title>""" + STYLE + """
</head><body><div class="wrap"><h1>🎌 Anime Auto Post Bot V3</h1>
<p><a href="/logout">Logout</a> · <a href="/health">Health</a></p>
<div class="card"><h2>➕ Add / update anime route</h2>
<form method="post" action="/route">
<input name="anime_name" placeholder="Anime name" required>
<input name="channel_id" placeholder="Destination channel ID" required>
<button>Save route</button></form></div>
<div class="card"><h2>🔗 Set anime channel link</h2>
<form method="post" action="/link">
<input name="anime_name" placeholder="Anime name" required>
<input name="channel_link" placeholder="https://t.me/channel" required>
<button>Save link</button></form></div>
<div class="card"><h2>🖼️ Set anime poster image</h2>
<form method="post" action="/poster-setting">
<input name="anime_name" placeholder="Anime name" required>
<input name="poster_url" placeholder="https://image-url" required>
<button>Save poster</button></form></div>
<div class="card"><h2>📋 Anime routes</h2><table><tr><th>Anime</th><th>Channel ID</th></tr>
{% for row in routes %}<tr><td>{{ row.anime_name }}</td><td>{{ row.channel_id }}</td></tr>{% endfor %}
</table></div>
<div class="card"><h2>🗄️ Backup</h2><a href="/backup">Download MongoDB JSON backup</a></div>
</div></body></html>"""


def logged_in():
    return bool(session.get("dashboard_logged_in"))


@web.route("/", methods=["GET"])
def home():
    return redirect("/dashboard") if logged_in() else (
        "Anime Auto Post Bot V3 is running. Open /login.", 200
    )


@web.route("/health", methods=["GET"])
def health():
    try:
        mongo_client.admin.command("ping")
        return {"status": "ok", "database": "connected"}, 200
    except Exception:
        return {"status": "degraded", "database": "unavailable"}, 503


@web.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        return "Set DASHBOARD_PASSWORD in Render environment variables.", 503
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, DASHBOARD_PASSWORD):
            session.clear()
            session["dashboard_logged_in"] = True
            return redirect("/dashboard")
        return LOGIN_PAGE, 401
    return LOGIN_PAGE


@web.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@web.route("/dashboard")
def dashboard():
    if not logged_in():
        return redirect("/login")
    routes = list(routes_col.find({}).sort("anime_name", ASCENDING).limit(300))
    return render_template_string(DASHBOARD_PAGE, routes=routes)


@web.route("/route", methods=["POST"])
def dashboard_route():
    if not logged_in():
        return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    try:
        channel = parse_channel_id(request.form.get("channel_id", ""))
        if not name:
            raise ValueError("Anime name is required.")
        routes_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name), "anime_name": name,
                "channel_id": channel, "updated_at": time.time(),
            }},
            upsert=True,
        )
    except Exception:
        logger.exception("Dashboard route update failed.")
    return redirect("/dashboard")


@web.route("/link", methods=["POST"])
def dashboard_link():
    if not logged_in():
        return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    try:
        link = validate_telegram_link(request.form.get("channel_link", ""))
        if not name:
            raise ValueError("Anime name is required.")
        links_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name), "anime_name": name,
                "channel_link": link, "updated_at": time.time(),
            }},
            upsert=True,
        )
    except Exception:
        logger.exception("Dashboard link update failed.")
    return redirect("/dashboard")


@web.route("/poster-setting", methods=["POST"])
def dashboard_poster():
    if not logged_in():
        return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    try:
        poster_url = validate_http_url(request.form.get("poster_url", ""))
        if not name:
            raise ValueError("Anime name is required.")
        posters_col.update_one(
            {"anime_key": normalize_name(name)},
            {"$set": {
                "anime_key": normalize_name(name), "anime_name": name,
                "poster_url": poster_url, "updated_at": time.time(),
            }},
            upsert=True,
        )
    except Exception:
        logger.exception("Dashboard poster update failed.")
    return redirect("/dashboard")


@web.route("/backup")
def dashboard_backup():
    if not logged_in():
        return redirect("/login")
    names = [
        "anime_routes", "bot_settings", "anime_posters",
        "anime_links", "episode_history", "admin_logs",
    ]
    payload = {
        "backup_version": 3,
        "created_at": time.time(),
        "database": MONGODB_DATABASE,
        "collections": {name: list(db[name].find({})) for name in names},
    }
    data = json_util.dumps(payload, indent=2).encode("utf-8")
    return send_file(
        io.BytesIO(data),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"anime_bot_backup_{int(time.time())}.json",
    )


@web.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def start_web_server():
    web.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


# =========================================================
# ERROR HANDLER AND MAIN
# =========================================================

async def error_handler(update, context):
    if context.error:
        logger.error(
            "Unhandled Telegram error",
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )


def main():
    init_db()
    migrate_sqlite()
    Thread(target=start_web_server, daemon=True, name="web-dashboard").start()

    app = Application.builder().token(BOT_TOKEN).build()

    command_map = {
        "start": start_command,
        "help": help_command,
        "status": status_command,
        "addanime": addanime,
        "removeanime": removeanime,
        "listanime": listanime,
        "setdefault": setdefault,
        "defaultchannel": defaultchannel,
        "setanimelink": setanimelink,
        "removeanimelink": removeanimelink,
        "listanimelinks": listanimelinks,
        "getanimelink": getanimelink,
        "setdefaultlink": setdefaultlink,
        "defaultlink": defaultlink,
        "setposter": setposter,
        "removeposter": removeposter,
        "listposters": listposters,
        "setdefaultposter": setdefaultposter,
        "searchanime": searchanime,
        "animeinfo": animeinfo,
        "stats": stats_command,
        "history": history_command,
        "clearhistory": clearhistory,
        "setcaption": setcaption,
        "defaultcaption": defaultcaption,
        "backup": backup_command,
        "restore": restore_command,
        "dashboard": dashboard_command,
    }
    for command, callback in command_map.items():
        app.add_handler(CommandHandler(command, callback))

    channel_filter = filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION)
    app.add_handler(
        MessageHandler(channel_filter, handle_channel_post),
        group=0,
    )
    app.add_handler(
        MessageHandler(channel_filter, process_source_post),
        group=1,
    )
    app.add_error_handler(error_handler)

    logger.info("Starting Anime Auto Post Bot V3.")
    app.run_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
