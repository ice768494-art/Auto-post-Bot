# Anime Auto Post Bot V2

## Included
- Existing Telegram episode-announcement forwarding and upload-channel routing
- MongoDB storage and optional SQLite import
- Duplicate episode protection with stale-pending recovery
- Generated 1280x720 episode posters stored in MongoDB
- Public direct poster URL: `https://YOUR-SERVICE.onrender.com/poster/ASSET_ID.jpg`
- Password-protected web admin dashboard
- Route, anime-link and poster URL management from dashboard
- JSON MongoDB backup and merge-restore
- Episode history, statistics and customizable upload captions

## Render environment variables

Required:
- `BOT_TOKEN`: Telegram bot token
- `MONGODB_URI`: MongoDB Atlas connection string
- `MAIN_CHANNEL_ID`: destination channel ID for announcements
- `SOURCE_CHANNEL_IDS`: comma-separated channel IDs/usernames to monitor for announcement posts
- `ADMIN_IDS`: comma-separated numeric Telegram user IDs

Upload routing:
- `SOURCE_CHANNEL_ID`: numeric ID of the upload source channel
- `MONGODB_DATABASE`: defaults to `anime_auto_post_bot`
- `MIGRATE_SQLITE`: defaults to true

Dashboard:
- `DASHBOARD_PASSWORD`: create a strong unique password
- `DASHBOARD_SECRET`: Render generates this automatically
- `PUBLIC_BASE_URL`: your Render service URL, e.g. `https://anime-auto-post-bot.onrender.com`

## Telegram bot permissions
1. Add the bot as an administrator to each source channel it must read.
2. Add it as an administrator to destination channels where it posts.
3. Give it permission to post messages and photos.
4. Set `ADMIN_IDS` to your own Telegram numeric user ID.

## Commands

General:
- `/start`, `/help`, `/status`

Routes:
- `/addanime Anime Name -1001234567890`
- `/removeanime Anime Name`
- `/listanime`
- `/setdefault -1001234567890`
- `/defaultchannel`

Anime links:
- `/setanimelink Anime Name https://t.me/channel`
- `/removeanimelink Anime Name`
- `/listanimelinks`
- `/getanimelink Anime Name`
- `/setdefaultlink https://t.me/channel`
- `/defaultlink`

Posters:
- `/setposter Anime Name https://image-url`
- `/removeposter Anime Name`
- `/listposters`
- `/setdefaultposter https://image-url`
- `/setdefaultposter off`

Tools:
- `/stats`
- `/searchanime Anime Name`
- `/animeinfo Anime Name`
- `/history [Anime Name]`
- `/clearhistory Anime Name`
- `/setcaption <b>{anime}</b> Season {season} Episode {episode}`
- `/defaultcaption`
- `/backup`
- Reply to a bot backup JSON document with `/restore`
- `/dashboard`

## Upload source post format

Example:
```text
The Ramparts Of Ice
Season 02
Episode 05
480P - https://example.com/file480
720P - https://example.com/file720
1080P - https://example.com/file1080
Image: https://example.com/poster.jpg
```

The image line is optional. If omitted, the bot tries the saved/built-in anime poster as the generated poster background, then creates a graphic poster if no image is available.

## Important notes
- A generated direct poster URL is public. Do not put private information in poster text.
- Generated poster image bytes are stored in MongoDB. Keep MongoDB storage limits and costs in mind.
- If `PUBLIC_BASE_URL` is blank, posters are still generated and posted to Telegram, but no direct URL button can be created. Set it to your Render service URL.
- The dashboard password is separate from Telegram `ADMIN_IDS`.
- Restore is a merge operation: it does not delete records absent from the backup.
- SQLite migration only works if the old SQLite file actually exists at `DATABASE_PATH` in the deployed environment. MongoDB does not recover a lost Render disk automatically.
- Render's filesystem is ephemeral unless a persistent disk is attached. This bot stores persistent bot data in MongoDB.
- This code is a deployment template and should be tested in a private Telegram channel before production use.
- Telegram's Bot API and external image hosts can fail temporarily. The bot logs errors to Render logs.
