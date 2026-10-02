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

If Instagram requires an account security check, PyVD retries public posts
without cookies. If that fails, it asks the bot owner to complete the check and
refresh the cookies. Instagram downloads prefer merged MP4 videos, matching the
gallery extractor's Telegram-compatible format.

Additional yt-dlp sites use their lowercase extractor family as `<site>` in
`private/cookies/<site>.txt` and `private/config.yaml`. Search `/extractors
<name>` to see that identifier. Site availability is set in
`private/config.yaml`; groups do not have per-site switches.

## BotFather setup

For inline links, use `/setinline` in BotFather to enable inline mode for the
bot. Use `/setinlinefeedback` at **100%** to start downloads when a result is
selected. If feedback is unavailable, the selected result has a Download
button that starts it. Leave `/setinlinegeo` disabled because PyVD does not
use location.

For automatic links in groups, disable group Privacy Mode with `/setprivacy`
and keep `/setjoingroups` enabled. Telegram then delivers ordinary group
messages to the bot. A bot made group admin receives all group messages even
with Privacy Mode enabled; grant admin rights only if link deletion is needed.

For compatible H.264/AAC videos from named yt-dlp providers and Instagram, PyVD
uploads Telegram parts while it downloads and remuxes the source. This includes
YouTube videos and Shorts with separate video and audio tracks, without reducing
the selected quality. Selected HTTP chunk sizes are honored; interrupted range
transfers resume with bounded retries and byte-range validation. Telegram sends
the message after the final part arrives.

Finite, unencrypted HLS and DASH sources can also stream through yt-dlp's native
fragment downloaders. Fragments are fetched sequentially with scoped cookies and
headers, with at most 32 MiB buffered per fragment. Cookie files are supported,
and Instagram uses its normal merged MP4 source. Galleries, albums, and formats
that need more processing use the completed-download path. Streaming never
selects a lower-quality format to make the upload start sooner.

Cached Telegram media is sent without waiting for a download slot. When
streaming falls back, PyVD reuses the extracted source details before fetching
the post again. These details and copied cookies stay in the temporary job
directory and are deleted when the job ends.

```sh
uv sync --locked --extra test --python 3.12
.venv/bin/python cmd/main.py --check
.venv/bin/python -m pytest -q
```

FFmpeg and ffprobe are required for local runs. Start with
`.venv/bin/python cmd/main.py` after the existing bot using the same token has
stopped.

## Owner health checks

Accounts listed in `ADMINS` can use `/health` in a DM with PyVD. It reports active
and queued download slots, local cookie expiration, and connectivity through
configured proxies. Cookie expiry cannot prove that a site still accepts the
login session. The command does not expose cookie values, account IDs, proxy
addresses, or download details, and sends no automatic notifications.

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

Send a supported link in a private chat or group. `/start` introduces the bot,
`/help` shows current usage, and `/extractors <name>` searches sites. These
commands, `/music`, and `/cancel` appear in the Telegram command menu; groups also show
`/settings`.
Fresh downloads run one per chat, with up to three chats downloading at once.
Queued chats take turns; cached media bypasses the download queue. Reply to your
link or `/music` request with `/cancel` to stop your queued or running job.
Request ownership exists only in memory while the job runs.

Confirmed text-only X posts are ignored; download and authentication failures
still receive an error reply. Photo-only X posts use the gallery extractor.
Reply to a video sent by PyVD with `/music` to receive its audio as a music
message. AAC and MP3 tracks keep their original quality; other audio tracks
are converted to MP3. The same file size and duration limits apply.
Extracted music is cached by the video’s Telegram identity. Repeated `/music`
requests reuse the uploaded audio without downloading or extracting it again.
Group admins can use `/settings` for captions, silent delivery, marked media,
album limits, and source link deletion. Inline mode supports one media item per
link. The group NSFW switch blocks media marked by source metadata or `#nsfw`
when disabled. When enabled, marked media is sent with a spoiler in public
groups and without an automatic spoiler in private groups. Use `#spoiler` to
request a spoiler separately. Sources without markers can still pass through.
NSFW markers on cached videos are retained by video ID. `/music` rejects known
marked videos when the group disallows them, and blocks their audio in public
groups because Telegram cannot hide audio with a spoiler.
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
