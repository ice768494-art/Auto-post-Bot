import os
import re
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================== LOGGING ====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

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

# ==================== ANIME CHANNEL LINKS ====================
# Add each anime's real channel link here.
# Names are matched without regard to capitalization.

ANIME_CHANNEL_LINKS = {
    "naruto shippuden": "https://t.me/+uKuAaVBzmSs0ZDll",
    "liar game": "https://t.me/+ki2AYsqJafs2NDk1",

    # Replace this example with your real Demon Slayer channel link.
    "demon slayer": "https://t.me/DemonSlayerChannel",

    # Add more anime links below.
    # "blue box": "https://t.me/YourBlueBoxChannel",
}

# Used when the detected anime doesn't match any entry.
# Replace with a real fallback channel link.
DEFAULT_CHANNEL_LINK = "https://t.me/YourDefaultChannel"

# ==================== ANIME POSTER URLS ====================

ANIME_POSTERS = {
    "naruto shippuden": "https://imgyx.pages.dev/VQw3e",
    "liar game": "https://imgyx.pages.dev/sjYWT",

    # Replace this example with a real, direct image URL.
    "blue box": "https://example.com/one-piece.jpg",
}

# Replace with a real, direct image URL.
DEFAULT_POSTER_URL = "https://example.com/default-anime.jpg"

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
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
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
    configured = {
        normalize_channel_id(channel_id)
        for channel_id in SOURCE_CHANNEL_IDS
    }

    if str(chat.id) in configured:
        return True

    username = getattr(chat, "username", None)

    if username:
        return normalize_channel_id(username) in configured

    return False


def contains_episode(text):
    return bool(
        re.search(r"\bepisode\b", text, flags=re.IGNORECASE)
    )


def get_source_link(message):
    chat = message.chat

    if chat.username:
        return (
            f"https://t.me/{chat.username}/"
            f"{message.message_id}"
        )

    chat_id = str(chat.id)

    if chat_id.startswith("-100"):
        internal_id = chat_id[4:]
        return (
            f"https://t.me/c/{internal_id}/"
            f"{message.message_id}"
        )

    return None


def get_message_text(message):
    return message.text or message.caption or ""


def get_anime_channel_link(anime_name):
    anime_name = anime_name.lower().strip()

    for anime, link in ANIME_CHANNEL_LINKS.items():
        if anime.lower() in anime_name:
            return link

    return DEFAULT_CHANNEL_LINK


def get_anime_poster_url(anime_name):
    anime_name = anime_name.lower().strip()

    for anime, poster_url in ANIME_POSTERS.items():
        if anime.lower() in anime_name:
            return poster_url

    return DEFAULT_POSTER_URL


def extract_episode_details(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    anime_name = "Unknown Anime"
    episode_number = "Unknown"

    for line in lines:
        match = re.search(
            r"\bEpisode\s*[:#-]?\s*(\d+(?:\.\d+)?)",
            line,
            flags=re.IGNORECASE,
        )

        if match:
            episode_number = match.group(1)
            break

    # Look for an anime title on the line before Episode.
    for index, line in enumerate(lines):
        if re.search(r"\bEpisode\b", line, re.IGNORECASE):
            if index > 0:
                candidate = lines[index - 1]

                if not re.search(
                    r"\b(480p|720p|1080p|2160p|Tamil|English|Japanese)\b",
                    candidate,
                    re.IGNORECASE,
                ):
                    anime_name = candidate
            break

    # Fallback: use the first line that isn't an episode or quality line.
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

            anime_name = line
            break

    return anime_name, episode_number


# ==================== COMMANDS ====================


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message:
        await update.effective_message.reply_text(
            "👋 Welcome to Anime Auto Post Bot!\n\n"
            "I detect channel posts containing the word Episode.\n"
            "Matching posts are announced in your main channel.\n\n"
            "Each anime can have its own channel link and poster.\n\n"
            "Use /status to check the bot."
        )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message:
        await update.effective_message.reply_text(
            "✅ Anime Auto Post Bot is running.\n\n"
            f"📢 Configured source channels: "
            f"{len(SOURCE_CHANNEL_IDS)}\n"
            "🔍 Keyword: Episode\n"
            "🗄️ Database: Not required"
        )


# ==================== EPISODE DETECTION ====================


async def handle_channel_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.channel_post

    if not message:
        return

    if not source_is_allowed(message.chat):
        return

    text = get_message_text(message)

    if not text or not contains_episode(text):
        return

    anime_name, episode_number = extract_episode_details(text)

    source_link = get_source_link(message)

    anime_link = get_anime_channel_link(anime_name)
    poster_url = get_anime_poster_url(anime_name)

    announcement = (
        "╭━━━『 🔥 NEW EPISODE 』━━━╮\n\n"
        f"🎬 Anime: {anime_name}\n"
        f"📺 Episode: {episode_number}\n"
        "🎧 Audio: Tamil\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔥 JOIN NOW 🔥",
                url=anime_link,
            )
        ]
    ])

    try:
        # Try posting with the anime-specific poster.
        if poster_url and not poster_url.startswith(
            "https://example.com/"
        ):
            try:
                await context.bot.send_photo(
                    chat_id=MAIN_CHANNEL_ID,
                    photo=poster_url,
                    caption=announcement,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.warning(
                    "Poster failed for %s; sending text instead.",
                    anime_name,
                    exc_info=True,
                )

                await context.bot.send_message(
                    chat_id=MAIN_CHANNEL_ID,
                    text=announcement,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
        else:
            # Don't try to send placeholder image URLs.
            await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=announcement,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        logger.info(
            "Episode %s announcement sent for %s",
            episode_number,
            anime_name,
        )

    except Exception:
        logger.exception("Failed to publish episode announcement")


# ==================== MAIN ====================


def main():
    Thread(
        target=start_web_server,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            handle_channel_post,
        )
    )

    logger.info("Anime Auto Post Bot starting...")

    application.run_polling(
        allowed_updates=[
            "message",
            "channel_post",
        ],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
