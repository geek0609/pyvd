from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import MediaError
from internal.core.tasks import JobRunner, delivery_spoiler
from internal.extractors.cookies import job_cookie_file
from internal.extractors.sites import (
    PUBLIC_GROUP_HOSTS, PUBLIC_GROUP_SITE_NAMES, Request, _named_extractors, _site_id,
    allowed_in_public_group, first_supported_url, identify, search_extractors,
)
from internal.models.media import ChatSettings, Media, MediaItem


def test_govd_site_ids() -> None:
    cases = {
        "https://youtu.be/YE7VzlLtp-4": ("youtube", "YE7VzlLtp-4"),
        "https://www.youtube.com/watch?v=1qLvAoo33UQ": ("youtube", "1qLvAoo33UQ"),
        "https://www.youtube.com/shorts/Z9NxQdQ8Rpg": ("youtube", "Z9NxQdQ8Rpg"),
        "https://www.instagram.com/p/Cabc123/": ("instagram", "Cabc123"),
        "https://www.tiktok.com/@user/video/123456": ("tiktok", "123456"),
        "https://x.com/user/status/123456": ("twitter", "123456"),
        "https://reddit.com/r/test/comments/abc123/title/": ("reddit", "abc123"),
        "https://www.pinterest.com/pin/123456/": ("pinterest", "123456"),
        "https://9gag.com/gag/abc123": ("ninegag", "abc123"),
        "https://www.threads.net/@user/post/abc123": ("threads", "abc123"),
    }
    for url, expected in cases.items():
        request = identify(url)
        assert request is not None
        assert (request.extractor_id, request.content_id) == expected
        assert allowed_in_public_group(request)


def test_public_group_rejects_unlisted_domains() -> None:
    assert set(PUBLIC_GROUP_HOSTS) == set(PUBLIC_GROUP_SITE_NAMES)
    assert not allowed_in_public_group(identify("https://vimeo.com/123456"))
    assert not allowed_in_public_group(identify("https://t.co/abc123"))
    assert not allowed_in_public_group(Request("youtube", "id", "https://vimeo.com/123456"))


def test_public_group_allowlist_includes_curated_sites() -> None:
    cases = {
        "pbskids": "https://pbskids.org/video/molly-of-denali/3030407927",
        "lego": "https://www.lego.com/en-us/videos/themes/club/blocumentary-kawaguchi-55492d823b1b4d5e985787fa8c2973b1",
        "nick.com": "https://www.nick.com/video-clips/0p4706/spongebob-squarepants-spongebob-loving-the-krusty-krab-for-7-minutes",
        "kika": "https://www.kika.de/kaltstart/videos/video92498",
        "toggo": "https://www.toggo.de/weihnachtsmann--co-kg/folge/ein-geschenk-fuer-zwei",
    }
    for site_id, url in cases.items():
        request = identify(url)
        assert request is not None
        assert request.extractor_id == site_id
        assert allowed_in_public_group(request)


def test_rejects_unrelated_or_local_urls() -> None:
    assert identify("https://notyoutube.com/watch?v=YE7VzlLtp-4") is None
    assert identify("http://127.0.0.1/video") is None
    assert identify("https://localhost/video") is None
    assert identify("https://example.invalid/video") is None
    assert identify("https://youtube.com:8443/watch?v=YE7VzlLtp-4") is None
    assert first_supported_url("hello https://www.instagram.com/p/Cabc123/ nice")


def test_telegram_links_are_ignored_before_extractor_matching() -> None:
    for host in (
        "t.me", "telegram.me", "telegram.dog", "telegram.org",
        "web.telegram.org", "telegra.ph", "graph.org", "telesco.pe",
    ):
        assert identify(f"https://{host}/example/123") is None, host
    assert identify("https://T.ME./example/123") is None
    assert first_supported_url("https://t.me/example/123") is None
    assert first_supported_url(
        "https://t.me/example/123 https://youtu.be/YE7VzlLtp-4"
    ).content_id == "YE7VzlLtp-4"


def test_known_sites_require_a_media_post_or_short_link() -> None:
    for url in (
        "https://www.youtube.com/", "https://www.youtube.com/@channel",
        "https://www.instagram.com/username/", "https://www.tiktok.com/@user",
        "https://x.com/username", "https://www.facebook.com/username",
        "https://www.reddit.com/r/test/", "https://www.pinterest.com/username/",
        "https://soundcloud.com/artist", "https://soundcloud.com/artist/sets/album",
        "https://9gag.com/", "https://www.threads.net/@username",
    ):
        assert identify(url) is None, url
    assert identify("https://t.co/abc123") is not None
    assert identify("https://vm.tiktok.com/ZMabcdef/") is not None
    assert identify("https://www.reddit.com/gallery/abc123") is not None
    assert first_supported_url(
        "https://x.com/username https://youtu.be/YE7VzlLtp-4"
    ).content_id == "YE7VzlLtp-4"


def test_named_ytdlp_extractors_are_recognized_without_changing_govd_ids() -> None:
    cases = {
        "https://vimeo.com/123456": ("vimeo", "123456"),
        "https://www.dailymotion.com/video/x8abcde": ("dailymotion", "x8abcde"),
        "https://www.twitch.tv/videos/123456789": ("twitch", "123456789"),
    }
    for url, (site_id, content_id) in cases.items():
        request = identify(url)
        assert request is not None
        assert (request.extractor_id, request.content_id) == (site_id, content_id)
        assert len(request.extractor_id) <= 30
        assert request.url == url
    assert identify("https://player.vimeo.com/video/123456").content_id == "123456"
    assert identify("https://youtu.be/YE7VzlLtp-4").extractor_id == "youtube"
    matches, total = search_extractors("vimeo")
    assert total >= 1
    assert ("vimeo", "vimeo") in matches


def test_all_named_extractor_ids_fit_govd_database() -> None:
    assert len(_named_extractors()) > 1000
    assert all(0 < len(_site_id(extractor)) <= 30 for extractor in _named_extractors())


def test_cookies_are_copied_not_modified(tmp_path: Path) -> None:
    private = tmp_path / "private" / "cookies"
    private.mkdir(parents=True)
    source = private / "instagram.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    work = tmp_path / "work"
    work.mkdir()
    copied = job_cookie_file(SimpleNamespace(cookie_path=lambda _: source), "instagram", work)
    assert copied is not None and copied.read_bytes() == source.read_bytes()
    copied.write_text("changed")
    assert source.read_text() == "# Netscape HTTP Cookie File\n"


@pytest.mark.asyncio
async def test_global_config_can_disable_all_additional_ytdlp_sites() -> None:
    settings = SimpleNamespace(
        site=lambda site_id: SimpleNamespace(
            disabled=site_id == "ytdlp", ignore_regex=(),
        ),
    )
    runner = JobRunner(SimpleNamespace(), settings, SimpleNamespace(), "pyvd")
    chat = ChatSettings(-100, "group", True, False, True, 10, False)
    with pytest.raises(MediaError, match="disabled"):
        await runner.run(Request("vimeo", "id", "https://vimeo.com/123456"), chat, -100)


@pytest.mark.asyncio
async def test_public_group_blocks_additional_site_before_lookup() -> None:
    runner = JobRunner(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), "pyvd")
    chat = ChatSettings(-100, "group", True, False, True, 10, False)
    with pytest.raises(MediaError, match="not in the public group allowlist"):
        await runner.run(
            Request("vimeo", "id", "https://vimeo.com/123456"), chat, -100,
            public_group=True,
        )


def test_marked_media_requires_group_permission_and_public_spoiler() -> None:
    video = Media(
        "youtube", "id", "https://youtu.be/YE7VzlLtp-4", nsfw=True,
        items=[MediaItem(kind="video", file_id="cached", video_codec="avc")],
    )
    blocked = ChatSettings(-100, "group", True, False, False, 10, False)
    allowed = ChatSettings(-100, "group", True, False, True, 10, False)
    with pytest.raises(MediaError, match="disabled"):
        delivery_spoiler(video, blocked, True, False)
    assert delivery_spoiler(video, allowed, True, False)
    assert not delivery_spoiler(video, allowed, False, False)
    with pytest.raises(MediaError, match="group inline mode"):
        delivery_spoiler(video, ChatSettings(123, "private", True, False, True, 10, False), True, False)


def test_tagged_media_obeys_group_setting() -> None:
    video = Media(
        "youtube", "id", "https://youtu.be/YE7VzlLtp-4",
        items=[MediaItem(kind="video", file_id="cached", video_codec="avc")],
    )
    blocked = ChatSettings(-100, "group", True, False, False, 10, False)
    allowed = ChatSettings(-100, "group", True, False, True, 10, False)
    with pytest.raises(MediaError, match="disabled"):
        delivery_spoiler(video, blocked, True, False, marked_nsfw=True)
    assert delivery_spoiler(video, allowed, True, False, marked_nsfw=True)
    assert not delivery_spoiler(video, allowed, False, False, marked_nsfw=True)
    assert not video.nsfw


def test_public_group_rejects_marked_media_without_spoiler_support() -> None:
    media = Media(
        "youtube", "id", "https://youtu.be/YE7VzlLtp-4", nsfw=True,
        items=[MediaItem(kind="audio", file_id="cached", audio_codec="mp3")],
    )
    allowed = ChatSettings(-100, "group", True, False, True, 10, False)
    with pytest.raises(MediaError, match="cannot be sent with a spoiler"):
        delivery_spoiler(media, allowed, True, False)


@pytest.mark.asyncio
async def test_cached_marked_video_is_sent_with_spoiler_in_public_group() -> None:
    media = Media(
        "youtube", "id", "", nsfw=True,
        items=[MediaItem(kind="video", file_id="cached", video_codec="avc")],
    )

    class Store:
        async def cached_media(self, extractor_id, content_id):
            assert (extractor_id, content_id) == ("youtube", "id")
            return media

    class Sender:
        async def send(self, chat_id, sent_media, caption, **kwargs):
            assert chat_id == -100 and sent_media is media
            assert sent_media.url == "https://youtu.be/YE7VzlLtp-4"
            assert kwargs["spoiler"] is True
            return []

    settings = SimpleNamespace(
        caching=True, captions_header="", captions_description="",
        site=lambda _: SimpleNamespace(disabled=False, ignore_regex=()),
    )
    runner = JobRunner(SimpleNamespace(), settings, Store(), "pyvd")
    runner.sender = Sender()
    chat = ChatSettings(-100, "group", False, False, True, 10, False)
    result = await runner.run(
        Request("youtube", "id", "https://youtu.be/YE7VzlLtp-4"), chat, -100,
        public_group=True,
    )
    assert result.media is media


@pytest.mark.asyncio
async def test_user_nsfw_marker_is_kept_with_cached_video_id() -> None:
    media = Media(
        "youtube", "id", "", items=[MediaItem(kind="video", file_id="cached", video_codec="avc")],
    )
    marked = []

    class Store:
        async def cached_media(self, extractor_id, content_id):
            return media

        async def mark_media_nsfw(self, extractor_id, content_id):
            marked.append((extractor_id, content_id))

    class Sender:
        async def send(self, chat_id, sent_media, caption, **kwargs):
            assert kwargs["spoiler"] is True
            return []

    settings = SimpleNamespace(
        caching=True, captions_header="", captions_description="",
        site=lambda _: SimpleNamespace(disabled=False, ignore_regex=()),
    )
    runner = JobRunner(SimpleNamespace(), settings, Store(), "pyvd")
    runner.sender = Sender()
    chat = ChatSettings(-100, "group", False, False, True, 10, False)
    await runner.run(
        Request("youtube", "id", "https://youtu.be/YE7VzlLtp-4"), chat, -100,
        public_group=True, marked_nsfw=True,
    )
    assert marked == [("youtube", "id")]
    assert media.nsfw
