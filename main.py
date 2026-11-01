import os
import re
import io
import json
import time
import html
import logging
import sqlite3
import secrets
import hashlib
from pathlib import Path
from threading import Thread
from contextlib import closing
from urllib.parse import urlparse
from datetime import datetime, timezone

import requests
from bson import json_util, ObjectId
from bson.binary import Binary
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from flask import (
    Flask, request, redirect, url_for, session, send_file,
    render_template_string, abort, flash
)
from PIL import Image, ImageDraw, ImageFont, ImageOps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# =========================================================
# LOGGING / CONFIG
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("anime_auto_post_bot")

STARTED_AT = time.time()
METRICS = {
    "episode_posts_ok": 0, "episode_posts_failed": 0,
    "upload_posts_ok": 0, "upload_posts_failed": 0,
}

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
MIGRATE_SQLITE = os.getenv("MIGRATE_SQLITE", "true").lower() in ("true", "1", "yes")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "").strip() or secrets.token_hex(32)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if not PUBLIC_BASE_URL and RENDER_HOST:
    PUBLIC_BASE_URL = "https://" + RENDER_HOST

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing.")
if not MAIN_CHANNEL_ID:
    raise ValueError("MAIN_CHANNEL_ID is missing.")
if not SOURCE_CHANNEL_IDS:
    raise ValueError("SOURCE_CHANNEL_IDS is missing.")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI is missing.")

# =========================================================
# BUILT-IN ANIME DATA (preserved from your current bot)
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
    MONGODB_URI, serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000, retryWrites=True,
)
db = mongo_client[MONGODB_DATABASE]
routes_col = db["anime_routes"]
settings_col = db["bot_settings"]
posters_col = db["anime_posters"]
links_col = db["anime_links"]
history_col = db["episode_history"]
logs_col = db["admin_logs"]
assets_col = db["poster_assets"]

def normalize_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

def init_db():
    mongo_client.admin.command("ping")
    for col in (routes_col, posters_col, links_col):
        col.create_index([("anime_key", ASCENDING)], unique=True)
    history_col.create_index([("created_at", DESCENDING)])
    history_col.create_index([("status", ASCENDING)])
    logs_col.create_index([("created_at", DESCENDING)])
    assets_col.create_index([("created_at", DESCENDING)])
    logger.info("MongoDB connected; indexes ready.")

def migrate_sqlite():
    if not MIGRATE_SQLITE or not os.path.isfile(DATABASE_PATH):
        logger.info("SQLite migration skipped.")
        return
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
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
        logger.info("SQLite import completed; original file retained.")
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

# =========================================================
# LOOKUPS / VALIDATION
# =========================================================

def get_matching_value(mapping, anime_name):
    normalized = normalize_name(anime_name)
    if normalized in mapping:
        return mapping[normalized]
    matches = [
        (normalize_name(title), value) for title, value in mapping.items()
        if normalize_name(title) in normalized or normalized in normalize_name(title)
    ]
    return sorted(matches, key=lambda x: len(x[0]), reverse=True)[0][1] if matches else None

def find_anime_document(collection, anime_name):
    normalized = normalize_name(anime_name)
    exact = collection.find_one({"anime_key": normalized})
    if exact:
        return exact
    docs = list(collection.find({}, limit=5000))
    matches = []
    for doc in docs:
        key = normalize_name(doc.get("anime_name", ""))
        if key and (key in normalized or normalized in key):
            matches.append(doc)
    return sorted(
        matches, key=lambda d: len(normalize_name(d.get("anime_name", ""))), reverse=True
    )[0] if matches else None

def get_anime_channel_link(anime_name):
    doc = find_anime_document(links_col, anime_name)
    return (doc.get("channel_link") if doc else None) or (
        get_matching_value(ANIME_CHANNEL_LINKS, anime_name)
        or get_setting("default_channel_link") or DEFAULT_CHANNEL_LINK
    )

def get_anime_poster_url(anime_name):
    doc = find_anime_document(posters_col, anime_name)
    return (doc.get("poster_url") if doc else None) or (
        get_matching_value(ANIME_POSTERS, anime_name)
        or get_setting("default_poster_url") or DEFAULT_POSTER_URL or None
    )

def get_route(anime_name):
    return find_anime_document(routes_col, anime_name)

def parse_channel_id(value):
    value = str(value).strip()
    if not re.fullmatch(r"-?\d+", value):
        raise ValueError("Use a numeric Telegram channel ID, for example -1001234567890.")
    return int(value)

def validate_http_url(value):
    value = str(value).strip()
    p = urlparse(value)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("Please provide a valid HTTP/HTTPS URL.")
    return value

def validate_telegram_link(value):
    value = validate_http_url(value)
    if urlparse(value).hostname not in ("t.me", "www.t.me", "telegram.me", "www.telegram.me"):
        raise ValueError("Please provide a Telegram channel link.")
    return value

def normalize_channel_id(value):
    value = str(value).strip()
    try:
        return str(int(value))
    except ValueError:
        return value.lstrip("@").lower()

def source_is_allowed(chat):
    configured = {normalize_channel_id(x) for x in SOURCE_CHANNEL_IDS}
    if str(chat.id) in configured:
        return True
    username = getattr(chat, "username", None)
    return bool(username and normalize_channel_id(username) in configured)

# =========================================================
# POSTER GENERATOR + PUBLIC URL
# =========================================================

def load_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()

def create_episode_poster(anime_name, season, episode, background_url=None):
    width, height = 1280, 720
    image = None
    if background_url:
        try:
            response = requests.get(
                background_url, timeout=12,
                headers={"User-Agent": "AnimeAutoPostBot/2.0"},
            )
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            image = ImageOps.fit(image, (width, height))
        except Exception:
            logger.warning("Could not load poster background; generating graphic poster.")
    if image is None:
        image = Image.new("RGB", (width, height), (18, 22, 42))
    overlay = Image.new("RGBA", (width, height), (5, 8, 20, 145))
    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((55, 55, width - 55, height - 55), radius=28,
                           outline=(255, 255, 255, 190), width=3)
    draw.text((95, 115), "ANIME NETWORK TAMIL", font=load_font(30, True),
              fill=(255, 220, 120, 255))
    draw.line((95, 170, width - 95, 170), fill=(255, 255, 255, 150), width=2)
    title = str(anime_name)[:72]
    # Wrap title into two lines, keeping the poster readable.
    words, lines, current = title.split(), [], ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textbbox((0, 0), test, font=load_font(58, True))[2] > width - 190 and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    y = 235
    for line in lines[:2]:
        draw.text((95, y), line, font=load_font(58, True), fill=(255, 255, 255, 255),
                  stroke_width=2, stroke_fill=(0, 0, 0, 180))
        y += 78
    draw.rounded_rectangle((95, 500, 650, 610), radius=18, fill=(255, 190, 70, 235))
    draw.text((125, 525), f"SEASON {str(season).zfill(2)}  •  EPISODE {episode}",
              font=load_font(28, True), fill=(20, 24, 40, 255))
    draw.text((95, 640), "TAMIL AUDIO  •  MULTI QUALITY",
              font=load_font(23, True), fill=(255, 255, 255, 230))
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue()

def save_poster_asset(image_bytes, anime_name, season, episode):
    result = assets_col.insert_one({
        "image": Binary(image_bytes),
        "anime_name": anime_name,
        "season": str(season),
        "episode": str(episode),
        "created_at": time.time(),
    })
    return str(result.inserted_id)

def public_poster_url(asset_id):
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/poster/{asset_id}.jpg"

# =========================================================
# UPLOAD PARSER / CAPTION
# =========================================================

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
        m = image_re.match(line)
        if m:
            candidate = m.group(1).rstrip(".,;)")
            try:
                image_url = validate_http_url(candidate)
            except ValueError:
                pass
        else:
            lines.append(line)
    if len(lines) < 2:
        return None

    season_re = re.compile(r"\b(?:season|s)\s*[-:#]?\s*(\d+)\b", re.I)
    season = "1"
    for line in lines:
        m = season_re.search(line)
        if m:
            season = m.group(1)
            break

    episode_re = re.compile(
        r"\b(?:(?:season|s)\s*\d+\s*[-:]?\s*)?(?:episode|ep|e)\s*[-:#]?\s*(\d+(?:\.\d+)?)\b",
        re.I,
    )
    episode_index, episode = None, None
    for i, line in enumerate(lines):
        m = episode_re.search(line)
        if m:
            episode_index, episode = i, m.group(1)
            break
    if episode_index is None:
        return None

    anime_name = " ".join(lines[:episode_index])
    anime_name = season_re.sub("", anime_name).strip(" \t-–—:|")
    anime_name = re.sub(r"^(?:anime|title)\s*[:=-]\s*", "", anime_name, flags=re.I).strip()
    if not anime_name:
        return None

    quality_re = re.compile(
        r"\b(144|240|360|480|540|576|720|1080|1440|2160)\s*[pP]\b"
        r"\s*[-:–—]?\s*(https?://[^\s|<>]+)", re.I
    )
    qualities = {}
    for line in lines[episode_index:]:
        for m in quality_re.finditer(line):
            quality = f"{m.group(1)}P"
            url = m.group(2).rstrip(".,;)")
            try:
                qualities[quality] = validate_http_url(url)
            except ValueError:
                pass
    if not qualities:
        return None
    return {
        "anime_name": anime_name,
        "season_number": season,
        "episode_number": episode,
        "qualities": qualities,
        "image_url": image_url,
    }

def make_caption(anime_name, season, episode):
    template = get_setting("upload_caption_template") or (
        "<b>{anime}</b> | தமிழ் Official Dub\n\n"
        "Season : {season} | Episode : {episode}\n\n"
        "‼️ Note - Click The Below Button to Get Episodes 👇"
    )
    values = {
        "anime": html.escape(str(anime_name)),
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

def make_keyboard(qualities, poster_url=None):
    preferred = ["480P", "720P", "1080P", "360P", "240P", "144P",
                 "540P", "576P", "1440P", "2160P"]
    ordered = [q for q in preferred if q in qualities]
    ordered.extend(q for q in qualities if q not in ordered)
    buttons = [InlineKeyboardButton(f"📥 {q}", url=qualities[q]) for q in ordered]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    if poster_url:
        rows.append([InlineKeyboardButton("🖼️ Direct Poster URL", url=poster_url)])
    return InlineKeyboardMarkup(rows)

# =========================================================
# DUPLICATE / HISTORY MANAGEMENT
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
            {"$set": {"created_at": now, "retry_count": int(existing.get("retry_count", 0)) + 1}},
        )
        return result.modified_count == 1
    except PyMongoError:
        logger.exception("MongoDB reservation failed.")
        return False

def mark_episode_sent(key, telegram_message_id=None):
    history_col.update_one(
        {"_id": key},
        {"$set": {"status": "sent", "telegram_message_id": telegram_message_id,
                  "sent_at": time.time()}},
    )

def release_episode(key):
    history_col.delete_one({"_id": key, "status": "pending"})

def make_upload_key(anime, season, episode):
    return f"upload:{normalize_name(anime)}:s{season}:e{episode}"

def make_announcement_key(chat_id, message_id):
    return f"announcement:{chat_id}:{message_id}"

# =========================================================
# ADMIN AUTH
# =========================================================

async def is_admin(update):
    user, message = update.effective_user, update.effective_message
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
# TELEGRAM ADMIN COMMANDS
# =========================================================

async def start_command(update, context):
    await update.effective_message.reply_text(
        "🔥 Anime Auto Publisher V2 is running!\nUse /help for commands."
    )

async def help_command(update, context):
    await update.effective_message.reply_text(
        "🎌 ANIME AUTO POST BOT V2\n\n"
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
        "Web: /dashboard"
    )

async def status_command(update, context):
    try:
        mongo_client.admin.command("ping")
        db_state = "✅ Connected"
        route_count = routes_col.count_documents({})
        link_count = links_col.count_documents({})
        poster_count = posters_col.count_documents({})
    except Exception as exc:
        db_state = f"❌ Unavailable ({type(exc).__name__})"
        route_count = link_count = poster_count = 0
    uptime = max(0, int(time.time() - STARTED_AT))
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    await update.effective_message.reply_text(
        "🤖 ANIME AUTO POST BOT V2\n"
        f"🟢 Process: Running\n⏱️ Uptime: {h}h {m}m {s}s\n"
        f"🗄️ MongoDB: {db_state}\n"
        f"📢 Announcement sources: {len(SOURCE_CHANNEL_IDS)}\n"
        f"🎬 Upload source: {'Configured' if SOURCE_CHANNEL_ID else 'Not configured'}\n"
        f"🧭 Routes: {route_count} | 🔗 Links: {link_count} | 🖼️ Posters: {poster_count}\n"
        f"📤 Announcements: {METRICS['episode_posts_ok']} ok / {METRICS['episode_posts_failed']} failed\n"
        f"📤 Uploads: {METRICS['upload_posts_ok']} ok / {METRICS['upload_posts_failed']} failed"
    )

async def addanime(update, context):
    if not await is_admin(update): return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /addanime Anime Name -1001234567890"); return
    try:
        channel_id = parse_channel_id(context.args[-1])
        name = " ".join(context.args[:-1]).strip()
        chat = await context.bot.get_chat(channel_id)
        routes_col.update_one({"anime_key": normalize_name(name)}, {"$set": {
            "anime_key": normalize_name(name), "anime_name": name,
            "channel_id": channel_id, "updated_at": time.time()
        }}, upsert=True)
        log_admin_action(update.effective_user.id, "addanime", name)
        await update.effective_message.reply_text(
            f"✅ Route saved\n🎬 {name}\n📢 {chat.title or channel_id}\n🆔 {channel_id}"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Could not save route: {exc}")

async def removeanime(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeanime Anime Name"); return
    result = routes_col.delete_one({"anime_key": normalize_name(name)})
    await update.effective_message.reply_text("🗑️ Route removed." if result.deleted_count else "ℹ️ Route not found.")

async def listanime(update, context):
    if not await is_admin(update): return
    rows = list(routes_col.find({}).sort("anime_name", ASCENDING).limit(100))
    if not rows:
        await update.effective_message.reply_text("No anime routes registered."); return
    text = "📚 REGISTERED ROUTES\n\n" + "\n\n".join(
        f"🎬 {r.get('anime_name')}\n🆔 {r.get('channel_id')}" for r in rows
    )
    for i in range(0, len(text), 3500):
        await update.effective_message.reply_text(text[i:i+3500])

async def setdefault(update, context):
    if not await is_admin(update): return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefault -1001234567890"); return
    try:
        channel = parse_channel_id(context.args[0])
        chat = await context.bot.get_chat(channel)
        set_setting("default_channel_id", channel)
        await update.effective_message.reply_text(f"✅ Default channel set: {chat.title or channel}\n{channel}")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")

async def defaultchannel(update, context):
    if not await is_admin(update): return
    channel = get_setting("default_channel_id")
    if not channel:
        await update.effective_message.reply_text("No default channel. Use /setdefault."); return
    await update.effective_message.reply_text(f"📢 Default channel ID: {channel}")

async def setanimelink(update, context):
    if not await is_admin(update): return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /setanimelink Anime Name https://t.me/channel"); return
    name = " ".join(context.args[:-1]).strip()
    try:
        link = validate_telegram_link(context.args[-1])
        links_col.update_one({"anime_key": normalize_name(name)}, {"$set": {
            "anime_key": normalize_name(name), "anime_name": name,
            "channel_link": link, "updated_at": time.time()
        }}, upsert=True)
        await update.effective_message.reply_text(f"✅ Link saved for {name}\n{link}")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))

async def removeanimelink(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeanimelink Anime Name"); return
    result = links_col.delete_one({"anime_key": normalize_name(name)})
    await update.effective_message.reply_text("🗑️ Link removed." if result.deleted_count else "ℹ️ Link not found.")

async def listanimelinks(update, context):
    if not await is_admin(update): return
    rows = list(links_col.find({}).sort("anime_name", ASCENDING).limit(80))
    if not rows:
        await update.effective_message.reply_text("No custom anime links saved."); return
    text = "🎌 SAVED LINKS\n\n" + "\n\n".join(
        f"{r.get('anime_name')}\n{r.get('channel_link')}" for r in rows
    )
    for i in range(0, len(text), 3500):
        await update.effective_message.reply_text(text[i:i+3500], disable_web_page_preview=True)

async def getanimelink(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /getanimelink Anime Name"); return
    await update.effective_message.reply_text(
        f"🎬 {name}\n🔗 {get_anime_channel_link(name)}",
        disable_web_page_preview=True
    )

async def setdefaultlink(update, context):
    if not await is_admin(update): return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefaultlink https://t.me/channel"); return
    try:
        link = validate_telegram_link(context.args[0])
        set_setting("default_channel_link", link)
        await update.effective_message.reply_text("✅ Default channel link updated.")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))

async def defaultlink(update, context):
    if not await is_admin(update): return
    await update.effective_message.reply_text(
        get_setting("default_channel_link") or DEFAULT_CHANNEL_LINK,
        disable_web_page_preview=True
    )

async def setposter(update, context):
    if not await is_admin(update): return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /setposter Anime Name https://image-url"); return
    name = " ".join(context.args[:-1]).strip()
    try:
        url = validate_http_url(context.args[-1])
        posters_col.update_one({"anime_key": normalize_name(name)}, {"$set": {
            "anime_key": normalize_name(name), "anime_name": name,
            "poster_url": url, "updated_at": time.time()
        }}, upsert=True)
        await update.effective_message.reply_text(f"✅ Poster saved for {name}\n{url}")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))

async def removeposter(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeposter Anime Name"); return
    result = posters_col.delete_one({"anime_key": normalize_name(name)})
    await update.effective_message.reply_text("🗑️ Poster removed." if result.deleted_count else "ℹ️ Poster not found.")

async def listposters(update, context):
    if not await is_admin(update): return
    rows = list(posters_col.find({}).sort("anime_name", ASCENDING).limit(80))
    if not rows:
        await update.effective_message.reply_text("No custom posters saved."); return
    text = "🖼️ CUSTOM POSTERS\n\n" + "\n\n".join(
        f"{r.get('anime_name')}\n{r.get('poster_url')}" for r in rows
    )
    for i in range(0, len(text), 3500):
        await update.effective_message.reply_text(text[i:i+3500], disable_web_page_preview=True)

async def setdefaultposter(update, context):
    if not await is_admin(update): return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefaultposter URL_or_off"); return
    if context.args[0].lower() == "off":
        set_setting("default_poster_url", "")
        await update.effective_message.reply_text("✅ Default poster disabled."); return
    try:
        set_setting("default_poster_url", validate_http_url(context.args[0]))
        await update.effective_message.reply_text("✅ Default poster updated.")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))

async def searchanime(update, context):
    if not await is_admin(update): return
    query = normalize_name(" ".join(context.args))
    if not query:
        await update.effective_message.reply_text("Usage: /searchanime Anime Name"); return
    results = []
    for label, col, field in (
        ("Route", routes_col, "channel_id"),
        ("Link", links_col, "channel_link"),
        ("Poster", posters_col, "poster_url"),
    ):
        for doc in col.find({}, limit=5000):
            if query in normalize_name(doc.get("anime_name", "")):
                results.append(f"🎬 {doc.get('anime_name')}\n{label}: {doc.get(field)}")
    text = "🔎 RESULTS\n\n" + "\n\n".join(results) if results else "No matching anime settings."
    await update.effective_message.reply_text(text[:3900], disable_web_page_preview=True)

async def animeinfo(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /animeinfo Anime Name"); return
    route, link, poster = get_route(name), find_anime_document(links_col, name), find_anime_document(posters_col, name)
    await update.effective_message.reply_text(
        f"🎌 ANIME INFORMATION\n🎬 {name}\n\n"
        f"📢 Route: {route.get('channel_id') if route else 'Not configured'}\n"
        f"🔗 Link: {link.get('channel_link') if link else get_anime_channel_link(name)}\n"
        f"🖼️ Poster: {poster.get('poster_url') if poster else get_anime_poster_url(name) or 'Not configured'}",
        disable_web_page_preview=True
    )

async def stats_command(update, context):
    if not await is_admin(update): return
    await update.effective_message.reply_text(
        "📊 BOT STATISTICS\n\n"
        f"Routes: {routes_col.count_documents({})}\n"
        f"Custom links: {links_col.count_documents({})}\n"
        f"Custom posters: {posters_col.count_documents({})}\n"
        f"Sent history: {history_col.count_documents({'status':'sent'})}\n"
        f"Pending history: {history_col.count_documents({'status':'pending'})}\n"
        f"Poster assets: {assets_col.count_documents({})}\n"
        f"Upload posts: {METRICS['upload_posts_ok']} / failed {METRICS['upload_posts_failed']}\n"
        f"Announcements: {METRICS['episode_posts_ok']} / failed {METRICS['episode_posts_failed']}"
    )

async def history_command(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    query = {"status": "sent"}
    if name:
        query["anime_name"] = {"$regex": re.escape(name), "$options": "i"}
    rows = list(history_col.find(query).sort("sent_at", DESCENDING).limit(15))
    if not rows:
        await update.effective_message.reply_text("No matching sent episode history."); return
    lines = ["📚 RECENT EPISODE HISTORY\n"]
    for r in rows:
        lines.append(
            f"🎬 {r.get('anime_name','Unknown')} | S{r.get('season','?')} E{r.get('episode','?')}\n"
            f"📌 {r.get('type','?')} | {datetime.fromtimestamp(r.get('sent_at', r.get('created_at', time.time())), timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
    await update.effective_message.reply_text("\n\n".join(lines))

async def clearhistory(update, context):
    if not await is_admin(update): return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /clearhistory Anime Name"); return
    result = history_col.delete_many({"anime_name": {"$regex": re.escape(name), "$options": "i"}})
    await update.effective_message.reply_text(f"🧹 Removed {result.deleted_count} history records for {name}.")

async def setcaption(update, context):
    if not await is_admin(update): return
    template = " ".join(context.args).strip()
    if not template:
        await update.effective_message.reply_text(
            "Usage: /setcaption <b>{anime}</b> Season {season} Episode {episode}\n"
            "Placeholders: {anime}, {season}, {episode}"
        ); return
    try:
        template.format(anime="Anime", season="01", episode="01")
    except Exception:
        await update.effective_message.reply_text("❌ Invalid template. Use only {anime}, {season}, {episode}."); return
    set_setting("upload_caption_template", template)
    await update.effective_message.reply_text("✅ Upload caption template saved.")

async def defaultcaption(update, context):
    if not await is_admin(update): return
    set_setting("upload_caption_template", "")
    await update.effective_message.reply_text("✅ Default upload caption restored.")

async def backup_command(update, context):
    if not await is_admin(update): return
    names = ["anime_routes", "bot_settings", "anime_posters", "anime_links", "episode_history", "admin_logs"]
    payload = {"backup_version": 2, "created_at": time.time(), "database": MONGODB_DATABASE, "collections": {}}
    for name in names:
        payload["collections"][name] = list(db[name].find({}))
    raw = json_util.dumps(payload, indent=2).encode("utf-8")
    bio = io.BytesIO(raw)
    bio.name = f"anime_bot_backup_{int(time.time())}.json"
    await update.effective_message.reply_document(
        document=bio, caption="🗄️ MongoDB backup. Keep this file private."
    )

async def restore_command(update, context):
    if not await is_admin(update): return
    message = update.effective_message
    doc = message.reply_to_message.document if message.reply_to_message else None
    if not doc:
        await message.reply_text("Reply to a bot backup JSON file with /restore."); return
    file = await context.bot.get_file(doc.file_id)
    content = bytes(await file.download_as_bytearray())
    try:
        payload = json_util.loads(content.decode("utf-8"))
        collections = payload.get("collections", {})
        allowed = {"anime_routes", "bot_settings", "anime_posters", "anime_links", "episode_history", "admin_logs"}
        restored = 0
        for name, documents in collections.items():
            if name not in allowed or not isinstance(documents, list):
                continue
            for item in documents:
                if not isinstance(item, dict) or "_id" not in item:
                    continue
                db[name].replace_one({"_id": item["_id"]}, item, upsert=True)
                restored += 1
        await message.reply_text(f"✅ Restore merge completed. Records processed: {restored}\nExisting records not in the backup were kept.")
    except Exception as exc:
        logger.exception("Restore failed.")
        await message.reply_text(f"❌ Invalid backup or restore failed: {type(exc).__name__}")

async def dashboard_command(update, context):
    if not await is_admin(update): return
    if not PUBLIC_BASE_URL:
        await update.effective_message.reply_text(
            "Dashboard URL is not configured. Set PUBLIC_BASE_URL to your Render service URL."
        ); return
    await update.effective_message.reply_text(
        f"🖥️ Admin dashboard: {PUBLIC_BASE_URL}/dashboard\n"
        "Sign in with DASHBOARD_PASSWORD from Render environment variables."
    )

# =========================================================
# CHANNEL POST PROCESSORS
# =========================================================

def get_message_text(message):
    return message.text or message.caption or ""

def contains_episode(text):
    return bool(re.search(r"\b(?:episode|ep)\b", text, re.I))

async def process_source_post(update, context):
    message = update.effective_message
    if not message or not SOURCE_CHANNEL_ID or not update.effective_chat:
        return
    try:
        expected_id = int(SOURCE_CHANNEL_ID)
    except ValueError:
        logger.error("SOURCE_CHANNEL_ID must be a numeric channel ID.")
        return
    if update.effective_chat.id != expected_id:
        return
    parsed = parse_post(get_message_text(message))
    if not parsed:
        return

    anime, season, episode = parsed["anime_name"], parsed["season_number"], parsed["episode_number"]
    route = get_route(anime)
    if route:
        destination_id = int(route["channel_id"])
    else:
        default_id = get_setting("default_channel_id")
        if not default_id:
            logger.warning("No route/default destination for %s", anime)
            return
        destination_id = int(default_id)

    key = make_upload_key(anime, season, episode)
    if not reserve_episode(key, {
        "type": "upload", "anime_name": anime, "season": season, "episode": episode,
        "source_chat_id": update.effective_chat.id, "source_message_id": message.message_id,
        "destination_id": destination_id,
    }):
        logger.info("Duplicate or active upload ignored: %s", key)
        return

    caption = make_caption(anime, season, episode)
    image_url = parsed.get("image_url") or get_anime_poster_url(anime)
    asset_id, poster_url = None, None
    try:
        poster_bytes = create_episode_poster(anime, season, episode, image_url)
        asset_id = save_poster_asset(poster_bytes, anime, season, episode)
        poster_url = public_poster_url(asset_id)
        keyboard = make_keyboard(parsed["qualities"], poster_url)
        sent = await context.bot.send_photo(
            chat_id=destination_id, photo=io.BytesIO(poster_bytes),
            caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
        mark_episode_sent(key, sent.message_id)
        METRICS["upload_posts_ok"] += 1
        logger.info("Upload posted: %s S%s E%s", anime, season, episode)
    except Exception:
        release_episode(key)
        METRICS["upload_posts_failed"] += 1
        logger.exception("Upload publication failed for %s", anime)

def extract_episode_details(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    anime, episode, season = "Unknown Anime", "Unknown", "01"
    for line in lines:
        m = re.search(r"\bEpisode\s*[:#-]?\s*(\d+(?:\.\d+)?)", line, re.I)
        if m: episode = m.group(1)
        m = re.search(r"\bSeason\s*[:#-]?\s*(\d+)", line, re.I)
        if m: season = m.group(1).zfill(2)
    for i, line in enumerate(lines):
        if re.search(r"\bEpisode\b", line, re.I):
            if i > 0:
                candidate = lines[i-1]
                if not re.search(r"\b(480p|720p|1080p|2160p|Tamil|English|Japanese|Season)\b", candidate, re.I):
                    anime = candidate.strip("➪➥➤-:| ")
            break
    if anime == "Unknown Anime":
        for line in lines:
            if re.search(r"\b(Episode|Season|480p|720p|1080p|2160p|Tamil|English|Japanese)\b", line, re.I):
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
    key = make_announcement_key(source.chat.id, source.message_id)
    if not reserve_episode(key, {
        "type": "announcement", "anime_name": anime, "season": season, "episode": episode,
        "source_chat_id": source.chat.id, "source_message_id": source.message_id,
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
    keyboard = InlineKeyboardMarkup([
    [
        InlineKeyboardButton(
            "⎙• ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴊᴏɪɴ •⎙",
            url=get_anime_channel_link(anime),
            style="success"
        )
    ],
    [
        InlineKeyboardButton(
            "↪ ꜱʜᴀʀᴇ ᴄʜᴀɴɴᴇʟ",
            url="https://t.me/AnimeNetworkTamil",
            style="primary"
        ),
        InlineKeyboardButton(
            "⚠️ ᴀɴʏ ɪꜱꜱᴜᴇꜱ ⚠️",
            url="https://t.me/Tamil_Animes_New",
            style="danger"
        ),
    ],
    ])
    poster = get_anime_poster_url(anime)
    try:
        sent = None
        if poster:
            try:
                sent = await context.bot.send_photo(
                    chat_id=MAIN_CHANNEL_ID, photo=poster, caption=announcement,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Announcement image failed; falling back to text.")
        if not sent:
            sent = await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID, text=announcement,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        mark_episode_sent(key, sent.message_id)
        METRICS["episode_posts_ok"] += 1
    except Exception:
        release_episode(key)
        METRICS["episode_posts_failed"] += 1
        logger.exception("Announcement failed for %s", anime)

# =========================================================
# FLASK ADMIN DASHBOARD + POSTER SERVING
# =========================================================

web = Flask(__name__)
web.secret_key = DASHBOARD_SECRET
web.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)

BASE_STYLE = """
<style>
body{font-family:Arial,sans-serif;background:#101827;color:#edf2f7;margin:0;padding:22px}
.wrap{max-width:1050px;margin:auto}.card{background:#1c293b;padding:18px;border-radius:14px;margin:14px 0}
h1,h2{margin-top:0}input,button{padding:11px;border-radius:8px;border:0;margin:5px 0;max-width:100%}
input{background:#edf2f7;color:#111;width:95%}button{background:#f2b84b;color:#111;font-weight:bold;cursor:pointer}
a{color:#ffd27a}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid #435064;overflow-wrap:anywhere}
.small{color:#b7c4d5;font-size:13px}.danger{color:#ff9b9b}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
</style>
"""

LOGIN_HTML = """<!doctype html><html><head><title>Anime Bot Login</title>""" + BASE_STYLE + """</head>
<body><div class="wrap"><div class="card"><h1>🎌 Anime Bot V2</h1><p>Admin dashboard sign in</p>
<form method="post"><input type="password" name="password" placeholder="Dashboard password" required>
<button type="submit">Sign in</button></form><p class="small">Only the bot administrator should access this page.</p></div></div></body></html>"""

DASHBOARD_HTML = """<!doctype html><html><head><title>Anime Bot Dashboard</title>""" + BASE_STYLE + """</head>
<body><div class="wrap"><h1>🎌 Anime Auto Post Bot V2</h1>
<p><a href="/logout">Logout</a> · <a href="/health">Health</a></p>
<div class="grid">
<div class="card"><h2>🧭 Routes</h2><b>{{ route_count }}</b></div>
<div class="card"><h2>🔗 Anime links</h2><b>{{ link_count }}</b></div>
<div class="card"><h2>🖼️ Posters</h2><b>{{ poster_count }}</b></div>
<div class="card"><h2>📚 Sent episodes</h2><b>{{ sent_count }}</b></div>
</div>
<div class="card"><h2>➕ Add / update anime route</h2>
<form method="post" action="/dashboard/route">
<input name="anime_name" placeholder="Anime name" required>
<input name="channel_id" placeholder="Destination channel ID e.g. -1001234567890" required>
<button>Save route</button></form></div>
<div class="card"><h2>🔗 Set anime channel link</h2>
<form method="post" action="/dashboard/link">
<input name="anime_name" placeholder="Anime name" required>
<input name="channel_link" placeholder="https://t.me/channel" required>
<button>Save link</button></form></div>
<div class="card"><h2>🖼️ Set anime poster URL</h2>
<form method="post" action="/dashboard/poster">
<input name="anime_name" placeholder="Anime name" required>
<input name="poster_url" placeholder="https://image-url" required>
<button>Save poster</button></form></div>
<div class="card"><h2>📋 Registered anime routes</h2>
<table><tr><th>Anime</th><th>Channel ID</th><th>Action</th></tr>
{% for r in routes %}<tr><td>{{ r.anime_name }}</td><td>{{ r.channel_id }}</td>
<td><form method="post" action="/dashboard/delete-route"><input type="hidden" name="anime_name" value="{{ r.anime_name }}"><button>Delete</button></form></td></tr>{% endfor %}
</table></div>
<div class="card"><h2>🗄️ Backup & restore</h2>
<p><a href="/dashboard/backup">Download JSON backup</a></p>
<form method="post" action="/dashboard/restore" enctype="multipart/form-data">
<input type="file" name="backup" accept=".json,application/json" required><button>Restore / merge backup</button></form>
<p class="small">Restore merges records; it does not delete records missing from the backup.</p></div>
<div class="card"><h2>🔗 Direct poster assets</h2><p class="small">Generated poster URLs are saved in MongoDB and served from this app. Keep this Render service deployed for URLs to work.</p></div>
</div></body></html>"""

def dashboard_login_required():
    return bool(session.get("dashboard_logged_in"))

@web.route("/", methods=["GET"])
def home():
    return redirect("/dashboard") if dashboard_login_required() else (
        "Anime Auto Post Bot V2 is running. Open /login for admin dashboard.", 200
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
        supplied = request.form.get("password", "")
        if secrets.compare_digest(supplied, DASHBOARD_PASSWORD):
            session.clear()
            session["dashboard_logged_in"] = True
            return redirect("/dashboard")
        return LOGIN_HTML.replace("Admin dashboard sign in", "Incorrect password. Try again."), 401
    return LOGIN_HTML

@web.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@web.route("/dashboard", methods=["GET"])
def dashboard():
    if not dashboard_login_required(): return redirect("/login")
    routes = list(routes_col.find({}).sort("anime_name", ASCENDING).limit(300))
    return render_template_string(
        DASHBOARD_HTML, routes=routes,
        route_count=routes_col.count_documents({}),
        link_count=links_col.count_documents({}),
        poster_count=posters_col.count_documents({}),
        sent_count=history_col.count_documents({"status": "sent"}),
    )

@web.route("/dashboard/route", methods=["POST"])
def dashboard_route():
    if not dashboard_login_required(): return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    try:
        channel = parse_channel_id(request.form.get("channel_id", ""))
        if not name: raise ValueError("Anime name is required.")
        routes_col.update_one({"anime_key": normalize_name(name)}, {"$set": {
            "anime_key": normalize_name(name), "anime_name": name,
            "channel_id": channel, "updated_at": time.time()
        }}, upsert=True)
        flash("Route saved.")
    except Exception as exc:
        flash(f"Could not save route: {exc}")
    return redirect("/dashboard")

@web.route("/dashboard/link", methods=["POST"])
def dashboard_link():
    if not dashboard_login_required(): return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    try:
        link = validate_telegram_link(request.form.get("channel_link", ""))
        if not name: raise ValueError("Anime name is required.")
        links_col.update_one({"anime_key": normalize_name(name)}, {"$set": {
            "anime_key": normalize_name(name), "anime_name": name,
            "channel_link": link, "updated_at": time.time()
        }}, upsert=True)
        flash("Anime link saved.")
    except Exception as exc:
        flash(f"Could not save link: {exc}")
    return redirect("/dashboard")

@web.route("/dashboard/poster", methods=["POST"])
def dashboard_poster():
    if not dashboard_login_required(): return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    try:
        poster = validate_http_url(request.form.get("poster_url", ""))
        if not name: raise ValueError("Anime name is required.")
        posters_col.update_one({"anime_key": normalize_name(name)}, {"$set": {
            "anime_key": normalize_name(name), "anime_name": name,
            "poster_url": poster, "updated_at": time.time()
        }}, upsert=True)
        flash("Poster URL saved.")
    except Exception as exc:
        flash(f"Could not save poster: {exc}")
    return redirect("/dashboard")

@web.route("/dashboard/delete-route", methods=["POST"])
def dashboard_delete_route():
    if not dashboard_login_required(): return redirect("/login")
    name = request.form.get("anime_name", "").strip()
    routes_col.delete_one({"anime_key": normalize_name(name)})
    return redirect("/dashboard")

@web.route("/dashboard/backup", methods=["GET"])
def dashboard_backup():
    if not dashboard_login_required(): return redirect("/login")
    payload = {"backup_version": 2, "created_at": time.time(), "database": MONGODB_DATABASE, "collections": {}}
    for name in ("anime_routes", "bot_settings", "anime_posters", "anime_links", "episode_history", "admin_logs"):
        payload["collections"][name] = list(db[name].find({}))
    data = json_util.dumps(payload, indent=2).encode()
    return send_file(io.BytesIO(data), mimetype="application/json", as_attachment=True,
                     download_name=f"anime_bot_backup_{int(time.time())}.json")

@web.route("/dashboard/restore", methods=["POST"])
def dashboard_restore():
    if not dashboard_login_required(): return redirect("/login")
    upload = request.files.get("backup")
    if not upload: return redirect("/dashboard")
    try:
        payload = json_util.loads(upload.read().decode("utf-8"))
        allowed = {"anime_routes", "bot_settings", "anime_posters", "anime_links", "episode_history", "admin_logs"}
        count = 0
        for name, docs in payload.get("collections", {}).items():
            if name not in allowed or not isinstance(docs, list): continue
            for doc in docs:
                if isinstance(doc, dict) and "_id" in doc:
                    db[name].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                    count += 1
        flash(f"Restore merge completed: {count} records.")
    except Exception as exc:
        flash(f"Restore failed: {type(exc).__name__}")
    return redirect("/dashboard")

@web.route("/poster/<asset_id>.jpg", methods=["GET"])
def serve_poster(asset_id):
    try:
        oid = ObjectId(asset_id)
    except Exception:
        abort(404)
    doc = assets_col.find_one({"_id": oid}, {"image": 1})
    if not doc or not doc.get("image"): abort(404)
    return send_file(io.BytesIO(bytes(doc["image"])), mimetype="image/jpeg",
                     download_name=f"anime-poster-{asset_id}.jpg", max_age=86400)

@web.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

def start_web_server():
    web.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)

# =========================================================
# ERROR HANDLER / MAIN
# =========================================================

async def error_handler(update, context):
    if context.error:
        logger.error("Unhandled Telegram error", exc_info=(
            type(context.error), context.error, context.error.__traceback__
        ))

def main():
    init_db()
    migrate_sqlite()
    Thread(target=start_web_server, daemon=True, name="web-dashboard").start()

    app = Application.builder().token(BOT_TOKEN).build()

    command_map = {
        "start": start_command, "help": help_command, "status": status_command,
        "addanime": addanime, "removeanime": removeanime, "listanime": listanime,
        "setdefault": setdefault, "defaultchannel": defaultchannel,
        "setanimelink": setanimelink, "removeanimelink": removeanimelink,
        "listanimelinks": listanimelinks, "getanimelink": getanimelink,
        "setdefaultlink": setdefaultlink, "defaultlink": defaultlink,
        "setposter": setposter, "removeposter": removeposter,
        "listposters": listposters, "setdefaultposter": setdefaultposter,
        "searchanime": searchanime, "animeinfo": animeinfo, "stats": stats_command,
        "history": history_command, "clearhistory": clearhistory,
        "setcaption": setcaption, "defaultcaption": defaultcaption,
        "backup": backup_command, "restore": restore_command,
        "dashboard": dashboard_command,
    }
    for name, callback in command_map.items():
        app.add_handler(CommandHandler(name, callback))

    channel_filter = filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION)
    app.add_handler(MessageHandler(channel_filter, handle_channel_post), group=0)
    app.add_handler(MessageHandler(channel_filter, process_source_post), group=1)
    app.add_error_handler(error_handler)

    logger.info("Starting Anime Auto Post Bot V2.")
    app.run_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=False,
    )

if __name__ == "__main__":
    main()
