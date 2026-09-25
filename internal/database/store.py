"""Read and write govd's existing PostgreSQL schema."""

import hashlib
from datetime import datetime

import asyncpg

from internal.config.settings import Settings
from internal.models.media import ChatSettings, Media, MediaItem


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        self.pool = await asyncpg.create_pool(
            host=self.settings.db_host,
            port=self.settings.db_port,
            database=self.settings.db_name,
            user=self.settings.db_user,
            password=self.settings.db_password,
            min_size=1,
            max_size=10,
        )
        async with self.pool.acquire() as db:
            await db.fetchval("SELECT 1 FROM settings LIMIT 1")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("database is not open")
        return self.pool

    async def chat(self, chat_id: int, kind: str) -> ChatSettings:
        if kind not in {"private", "group"}:
            raise ValueError("invalid chat type")
        async with self._pool().acquire() as db, db.transaction():
            await db.execute(
                "INSERT INTO chat (chat_id, type) VALUES ($1, $2::chat_type) "
                "ON CONFLICT (chat_id) DO NOTHING",
                chat_id, kind,
            )
            await db.execute(
                "INSERT INTO settings (chat_id, language, captions, silent, nsfw, "
                "media_album_limit, delete_links) VALUES ($1, 'en', $2, $3, $4, $5, $6) "
                "ON CONFLICT (chat_id) DO NOTHING",
                chat_id,
                self.settings.default_captions,
                self.settings.default_silent,
                self.settings.default_nsfw,
                self.settings.default_media_album_limit,
                self.settings.default_delete_links,
            )
            row = await db.fetchrow(
                "SELECT c.chat_id, c.type::text AS kind, s.captions, s.silent, s.nsfw, "
                "s.media_album_limit, s.delete_links, s.disabled_extractors "
                "FROM chat c JOIN settings s ON s.chat_id = c.chat_id WHERE c.chat_id = $1",
                chat_id,
            )
        return ChatSettings(
            chat_id=row["chat_id"], kind=row["kind"], captions=row["captions"],
            silent=row["silent"], nsfw=row["nsfw"],
            media_album_limit=row["media_album_limit"],
            delete_links=row["delete_links"],
            disabled_extractors=tuple(row["disabled_extractors"]),
        )

    async def set_setting(self, chat_id: int, name: str, value: bool | int) -> None:
        allowed = {
            "captions": bool, "silent": bool, "nsfw": bool,
            "delete_links": bool, "media_album_limit": int,
        }
        if name not in allowed or type(value) is not allowed[name]:
            raise ValueError("invalid setting or value")
        if name == "media_album_limit" and value not in {1, 5, 10, 15, 20}:
            raise ValueError("invalid media album limit")
        await self._pool().execute(
            f"UPDATE settings SET {name} = $2, updated_at = NOW() WHERE chat_id = $1",
            chat_id, value,
        )

    async def set_extractor_enabled(self, chat_id: int, extractor_id: str, enabled: bool) -> None:
        if enabled:
            sql = "UPDATE settings SET disabled_extractors = array_remove(disabled_extractors, $2), updated_at = NOW() WHERE chat_id = $1"
        else:
            sql = (
                "UPDATE settings SET disabled_extractors = array_append(disabled_extractors, $2), "
                "updated_at = NOW() WHERE chat_id = $1 AND NOT ($2 = ANY(disabled_extractors))"
            )
        await self._pool().execute(sql, chat_id, extractor_id)

    async def cached_media(self, extractor_id: str, content_id: str) -> Media | None:
        async with self._pool().acquire() as db:
            row = await db.fetchrow(
                "SELECT id, content_url, caption, nsfw FROM media "
                "WHERE extractor_id = $1 AND content_id = $2",
                extractor_id, content_id,
            )
            if row is None:
                return None
            item_rows = await db.fetch(
                "SELECT f.format_id, f.file_id, f.type::text AS kind, f.file_size, "
                "f.duration, f.width, f.height, f.title, f.artist, "
                "f.audio_codec::text AS audio_codec, f.video_codec::text AS video_codec, f.bitrate "
                "FROM media_item i JOIN media_format f ON f.item_id = i.id "
                "WHERE i.media_id = $1 ORDER BY i.id",
                row["id"],
            )
        if not item_rows:
            return None
        items = []
        for item in item_rows:
            items.append(MediaItem(
                kind=item["kind"], file_id=item["file_id"], format_id=item["format_id"],
                size=item["file_size"] or 0, duration=item["duration"] or 0,
                width=item["width"] or 0, height=item["height"] or 0,
                title=item["title"] or "", artist=item["artist"] or "",
                audio_codec=item["audio_codec"] or "",
                video_codec=item["video_codec"] or "", bitrate=item["bitrate"] or 0,
            ))
        return Media(
            extractor_id=extractor_id, content_id=content_id,
            url=row["content_url"], caption=row["caption"] or "",
            nsfw=row["nsfw"], items=items,
        )

    async def save_media(self, media: Media) -> None:
        if not media.items or any(not item.file_id for item in media.items):
            raise ValueError("cannot cache media without Telegram file IDs")
        async with self._pool().acquire() as db, db.transaction():
            media_id = await db.fetchval(
                "INSERT INTO media (content_id, content_url, extractor_id, caption, nsfw) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (content_id, extractor_id) DO UPDATE SET "
                "content_url = EXCLUDED.content_url, caption = EXCLUDED.caption, "
                "nsfw = EXCLUDED.nsfw, updated_at = NOW() RETURNING id",
                media.content_id, media.url, media.extractor_id, media.caption or None, media.nsfw,
            )
            await db.execute("DELETE FROM media_item WHERE media_id = $1", media_id)
            for item in media.items:
                item_id = await db.fetchval(
                    "INSERT INTO media_item (media_id) VALUES ($1) RETURNING id", media_id,
                )
                await db.execute(
                    "INSERT INTO media_format (format_id, item_id, file_id, type, "
                    "audio_codec, video_codec, duration, file_size, title, artist, width, height, bitrate) "
                    "VALUES ($1, $2, $3, $4::media_type, $5::media_codec, $6::media_codec, "
                    "$7, $8, $9, $10, $11, $12, $13)",
                    item.format_id[:100], item_id, item.file_id, item.kind,
                    item.audio_codec or None, item.video_codec or None,
                    item.duration or None, item.size or None, item.title or None,
                    item.artist or None, item.width or None, item.height or None,
                    item.bitrate or None,
                )

    async def stats(self, since: datetime) -> dict[str, int]:
        row = await self._pool().fetchrow(
            "SELECT "
            "(SELECT count(*) FROM chat WHERE type = 'private' AND created_at >= $1) AS private_chats, "
            "(SELECT count(*) FROM chat WHERE type = 'group' AND created_at >= $1) AS group_chats, "
            "(SELECT count(*) FROM media_format f JOIN media_item i ON i.id = f.item_id "
            "JOIN media m ON m.id = i.media_id WHERE m.created_at >= $1) AS downloads, "
            "(SELECT coalesce(sum(f.file_size), 0) FROM media_format f JOIN media_item i ON i.id = f.item_id "
            "JOIN media m ON m.id = i.media_id WHERE m.created_at >= $1) AS bytes",
            since,
        )
        return dict(row)

    async def log_error(self, error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        error_id = hashlib.sha256(message.encode()).hexdigest()[:8]
        await self._pool().execute(
            "INSERT INTO errors (id, message) VALUES ($1, $2) "
            "ON CONFLICT (id) DO UPDATE SET occurrences = errors.occurrences + 1, last_seen = NOW()",
            error_id, message,
        )
        return error_id

    async def error(self, error_id: str) -> str | None:
        return await self._pool().fetchval("SELECT message FROM errors WHERE id = $1", error_id)
