# PyVD

PyVD is an English-only Telegram media bot built with Hydrogram. It accepts
links in private chats and groups, downloads with yt-dlp or gallery-dl, and
uploads files of up to 2,000,000,000 bytes through MTProto. It retains govd's
ten site families and recognizes HTTP(S) links handled by yt-dlp's named site
extractors. yt-dlp's Generic catch-all does not match incoming links. Site
availability still depends on upstream extractors, cookies, and the source site.

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

Additional yt-dlp sites use their lowercase extractor family as `<site>` in
`private/cookies/<site>.txt` and `private/config.yaml`. Search `/extractors
<name>` to see that identifier. Site availability is set in
`private/config.yaml`; groups do not have per-site switches.

For compatible H.264/AAC videos from YouTube, TikTok, X, and Facebook, PyVD
uploads Telegram parts while it downloads and remuxes the source. This includes
YouTube videos and Shorts with separate video and audio tracks, without reducing
the selected quality. Telegram sends the message after the final part arrives.
Other sites, formats that need more processing, albums, and posts using cookie
files use the normal completed-download path.

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
The new service is named `pyvd`. Job files are removed when each job ends, and
container logging is disabled. Rollback means stopping `pyvd` and starting the
original `bot`.

## Commands

Send a supported link in private chat. In a group, use `/download <link>` so
Telegram group privacy can stay enabled. `/start` introduces the bot, `/help`
shows current usage, and `/extractors <name>` searches sites. These commands
and `/music` appear in the Telegram command menu; groups also show `/settings`.
Reply to a video sent by PyVD with `/music` to receive its audio as a music
message. AAC and MP3 tracks keep their original quality; other audio tracks
are converted to MP3. The same file size and duration limits apply.
Group admins can use `/settings` for captions, silent delivery, marked media,
album limits, and source link deletion. Inline mode supports one media item per
link. The group NSFW switch blocks media marked by source metadata or `#nsfw`
when disabled. When enabled, marked media is sent with a spoiler in public
groups and without an automatic spoiler in private groups. Use `#spoiler` to
request a spoiler separately. Sources without markers can still pass through.
Public supergroups with a username have a fixed allowlist: the original ten
sites plus PBS Kids, LEGO, Nickelodeon, KiKA, and TOGGO. Only their direct
domains are accepted. General shorteners, including t.co, are blocked. Group
inline queries only offer allowlisted domains. Marked media is not delivered
through group inline mode because Telegram does not identify the target group
or expose its NSFW setting to the bot.

The reusable Telegram media cache is keyed by extractor and video ID, or a
digest of the source URL where the extractor has no usable ID. It contains no
downloader or chat ID, and source URLs are not stored in the cache. Private
chats use in-memory defaults rather than saved settings; only group settings
remain in the database. The bot does not keep error records or application logs.
