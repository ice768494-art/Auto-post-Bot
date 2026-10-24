import os
import re
import logging
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
ANIME_CHANNEL_LINKS = {
    "naruto shippuden": "https://t.me/+uKuAaVBzmSs0ZDll",
    "liar game": "https://t.me/+ki2AYsqJafs2NDk1",
    "the laid-off cheat-granting mage enjoys a second lease on life": "https://t.me/+Sij9-LGXOmFjYTA1",
    "Tomb Raider King" : "https://t.me/+1d2421FenfNlZGM9",
    "The Exiled Heavy Knight Knows How To Game The System": "https://t.me/+SppUueMG-bdiMjI1",
    "The Ramparts Of Ice" : "https://t.me/+eGLMJuQ2lTA4YTBl",
    "JoJo’s Bizarre Adventure Season 6 – Steel Ball Run" : "https://t.me/+4iT4nt1SFNE2Yzdl",
    "The Apothecary Diaries" : "https://t.me/+Ab5t_ExrlXkyOTNl",
    "A Wild Last Boss Appeared" : "https://t.me/+s6_zOzbnJ_UxMjk9",
    "Black Clover" : "https://t.me/+WTEdJxxK8OU0ODBl",
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
    "JoJo’s Bizarre Adventure Season 6 – Steel Ball Run" : "https://imgyx.pages.dev/NDUnT",
    "The Apothecary Diaries" : "https://imgyx.pages.dev/JaNj9",
    "A Wild Last Boss Appeared" : "https://imgyx.pages.dev/HGBe4",
    "Black Clover" : "https://imgyx.pages.dev/PQ8rI",
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
    return get_matching_value(ANIME_POSTERS, anime_name) or DEFAULT_POSTER_URL or None


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


# ==================== COMMANDS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "👋 Welcome to Anime Auto Post Bot!\n\n"
            "I detect channel posts containing the word Episode.\n"
            "Matching posts are announced in your main channel.\n\n"
            "Each anime can have its own channel link and poster.\n\n"
            "Use /status to check the bot."
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "✅ Anime Auto Post Bot is running.\n\n"
            f"📢 Configured source channels: {len(SOURCE_CHANNEL_IDS)}\n"
            "🔍 Keyword: Episode\n"
            "🗄️ Database: Not required"
        )


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
        f"<blockquote>➪ {escape(anime_name)}</blockquote>\n"
    
    "──────────────────\n"
    f"<b>➥ Season : {escape(season_number)}\n"   
    f"➤ Episode: {escape(episode_number)}\n"
    "➥ Quality: Multi Quality [Tamil Audio]</b>\n"
    "──────────────────\n"
    f"<blockquote>➽ NEW EPISODE {escape(episode_number)} ADDED 👇</blockquote>" )
  

    # Telegram Bot API does not support custom inline-button background colors.
    # This layout matches the screenshot: one full-width row, then two side-by-side.
    buttons = [
    [
        InlineKeyboardButton(
            "• ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴊᴏɪɴ •",
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

        logger.info(
            "Episode %s announcement sent for %s",
            episode_number,
            anime_name,
        )
    except Exception:
        logger.exception(
            "Failed to publish episode announcement to MAIN_CHANNEL_ID=%s",
            MAIN_CHANNEL_ID,
        )


# ==================== MAIN ====================
def main():
    Thread(target=start_web_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post)
    )

    logger.info("Anime Auto Post Bot starting...")
    application.run_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
