# PyVD

PyVD is an English-only Telegram media bot built with Hydrogram. It accepts
links in private chats and groups, downloads with yt-dlp or gallery-dl, and
uploads files of up to 2,000,000,000 bytes through MTProto. It recognizes the
same ten site families as govd: Facebook, Instagram, 9GAG, Pinterest, Reddit,
SoundCloud, Threads, TikTok, X, and YouTube. Site availability still depends
on upstream extractors, cookies, and the source site.

## Configuration

Use Python 3.12. Copy `.env.example` to `.env` and set `API_ID`, `API_HASH`,
`BOT_TOKEN`, and the existing govd PostgreSQL credentials there. `.env` is
ignored by Git. `MAX_FILE_SIZE` is in decimal MB and capped at 2000.

PyVD uses the govd `private/config.yaml` site overrides and
`private/cookies/<site>.txt` files. Its compose file mounts the active govd
`private` directory, `../govd/private`, by default. Set `GOVD_PRIVATE_DIR` in
`.env` if that directory is elsewhere. PyVD copies cookie files into each
download job so extractors cannot rewrite the originals. The existing govd
database is used directly; PyVD does not run migrations.

For compatible H.264/AAC videos, PyVD uploads Telegram parts while it downloads
and remuxes the source. This includes YouTube videos and Shorts with separate
video and audio tracks, without reducing the selected quality. Telegram sends
the message after the final part arrives. Formats that need other processing,
albums, and posts using cookie files use the normal completed-download path.

```sh
uv sync --locked --extra test --python 3.12
.venv/bin/python cmd/main.py --check
.venv/bin/python -m pytest -q
```

FFmpeg and ffprobe are required for local runs. Start with
`.venv/bin/python cmd/main.py` after the existing bot using the same token has
stopped.

## Oracle deployment layout

The compose file joins `govd_govd-network` to reach the existing `db`
container, and `proxy_net` for govd's configured proxy. On `oracle`, the
active govd private directory is `/home/ubuntu/govd/govd/private`. Keep that
directory and the `db` container in place when switching bots. The old
`bot-mtproto` container uses a separate private directory and is deprecated.
The image runs as root because the active cookie files are owned by UID 1001
and mode 0600, while the existing downloads directory is root-owned.

From the PyVD directory on the host, with `.env` populated, run
`docker compose build` and `docker compose run --rm pyvd python cmd/main.py --check`
to validate the image and configuration. Stop the old `bot` before
`docker compose up -d` so one process consumes updates for the shared token.
The new service is named `pyvd`; its downloads and logs remain under this
directory. Rollback means stopping `pyvd` and starting the original `bot`.

## Commands

Send a supported link in private chat or a group. `/extractors` lists sites.
Reply to a video sent by PyVD with `/music` to receive its audio as a music
message. AAC and MP3 tracks keep their original quality; other audio tracks
are converted to MP3. The same file size and duration limits apply.
Group admins can use `/settings` for captions, silent delivery, NSFW content,
album limits, disabled extractors, and source link deletion. Bot admins can
use `/stats` and `/derr <id>`. Inline mode supports one media item per link.
