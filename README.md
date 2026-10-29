# Combined Auto-post-Bot + ANuploadvot

This project combines the supplied `Auto-post-Bot` and `ANuploadvot` source files into one Python Telegram bot process using one `BOT_TOKEN`.

## Features

- Watches the comma-separated `SOURCE_CHANNEL_IDS` and posts formatted anime episode announcements to `MAIN_CHANNEL_ID`, using the configured anime links/poster URLs from the original Auto-post-Bot.
- Includes ANuploadvot's route-management commands and quality-link publisher.
- Stores anime destination routes, default destination, custom poster URLs, and the default poster in SQLite.
- Admins can manage posters directly in Telegram with `/setposter`, `/removeposter`, `/listposters`, and `/setdefaultposter`.
- `/status` reports uptime, database connectivity, route/poster counts, configured source channels, and process-lifetime publish success/failure counters.
- Logs detailed exceptions to Render logs while showing safer, shorter error messages to users.
- Runs one HTTP health server for Render and one Telegram polling loop.

**Note:** The supplied ANuploadvot source code implements upload/quality-link routing, but does not contain a voting feature. Voting cannot be preserved/added without its intended requirements or source code.

## Render environment variables

Required for Auto-post-Bot:
- `BOT_TOKEN`: token from BotFather.
- `MAIN_CHANNEL_ID`: numeric ID of the channel where Auto-post announcements go.
- `SOURCE_CHANNEL_IDS`: comma-separated numeric channel IDs or usernames that Auto-post-Bot should monitor, e.g. `-1001111111111,-1002222222222`.

For ANuploadvot upload routing:
- `SOURCE_CHANNEL_ID`: numeric ID of the one source channel whose posts should be parsed for anime name, episode, and quality links. It can be the same as one of `SOURCE_CHANNEL_IDS`.
- `ADMIN_IDS`: comma-separated numeric Telegram user IDs allowed to use route-management and poster-management commands.
- `DATABASE_PATH`: set to `/var/data/anime_channels.sqlite3`. `render.yaml` sets this automatically and mounts a persistent disk at `/var/data`.

`PORT` is supplied by Render; the code defaults to `10000` for local testing.

## How to use upload routing

1. Add the bot as an administrator in the source and destination channels.
2. Set your numeric Telegram user ID in `ADMIN_IDS`.
3. In private chat with the bot, use:
   - `/addanime Black Clover -1001234567890`
   - `/listanime`
   - `/removeanime Black Clover`
   - `/setdefault -1001234567890`
   - `/defaultchannel`
   - `/setposter Black Clover https://example.com/black-clover.jpg`
   - `/removeposter Black Clover`
   - `/listposters`
   - `/setdefaultposter https://example.com/default.jpg` (or `/setdefaultposter off`)
4. A text or media-caption source post can use a format like:

   Black Clover
   Episode 01
   480p - https://example.com/480p
   720p - https://example.com/720p

5. The bot publishes the episode caption with quality-link buttons to the anime's registered destination channel, or to the configured default destination.
6. Custom posters saved with `/setposter` take priority over built-in poster URLs. `/removeposter` removes only the custom override, so a built-in poster can be used again. The default poster is used only when no matching built-in or custom anime poster is found.

## Deployment

1. Upload these files to a new GitHub repository (or replace the files in a copy of one repository).
2. Create a Render Web Service from that repository.
3. Set the environment variables above.
4. Ensure the persistent disk is mounted at `/var/data` and set `DATABASE_PATH=/var/data/anime_channels.sqlite3`. Render persistent disks require a compatible paid service plan; the `plan: free` setting in a blueprint cannot use a persistent disk. If you stay on Free for temporary testing, set `DATABASE_PATH=/tmp/anime_channels.sqlite3`, but poster/routes/settings will not survive restarts or redeployments.
5. Deploy with `pip install -r requirements.txt` and start command `python main.py`.

`/status` publish counters are in-memory counters and reset when the bot restarts. Detailed exceptions are available in Render's service logs.

Do not commit real tokens or database credentials.
