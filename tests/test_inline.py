import json
import time
from types import SimpleNamespace

import pytest
from hydrogram import enums

from internal.bot.inline import Inline, _edit_media
from internal.bot.main import Bot
from internal.extractors.sites import Request
from internal.models.media import ChatSettings, Media, MediaItem


def test_inline_video_edit_uses_uploaded_video_file_id(monkeypatch) -> None:
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"ok":true,"result":true}'

    def urlopen(request, timeout):
        sent.update(json.loads(request.data))
        assert request.full_url.endswith("/editMessageMedia")
        assert timeout == 30
        return Response()

    monkeypatch.setattr("internal.bot.inline.urlopen", urlopen)
    item = MediaItem(
        kind="video", file_id="telegram-video-id", video_codec="avc",
        audio_codec="aac", duration=15, width=1920, height=1080,
    )
    _edit_media("test-token", "inline-message", item, "<b>source</b>")
    assert sent == {
        "inline_message_id": "inline-message",
        "media": {
            "type": "video", "media": "telegram-video-id",
            "caption": "<b>source</b>", "parse_mode": "HTML",
            "supports_streaming": True, "duration": 15,
            "width": 1920, "height": 1080,
        },
    }


def test_inline_tasks_expire_and_belong_to_user() -> None:
    inline = Inline(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    request = Request("youtube", "id", "https://youtu.be/id")
    task_id = inline.add(123, request)
    assert inline.pop(task_id, 999) is None
    task_id = inline.add(123, request)
    assert inline.pop(task_id, 123).request == request
    task_id = inline.add(123, request)
    inline.pending[task_id].expires = time.monotonic() - 1
    assert inline.pop(task_id, 123) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "url", "expected_results"),
    [
        (enums.ChatType.SUPERGROUP, "https://vimeo.com/123456", 0),
        (enums.ChatType.SUPERGROUP, "https://t.co/abc123", 0),
        (enums.ChatType.PRIVATE, "https://vimeo.com/123456", 1),
        (enums.ChatType.SUPERGROUP, "https://youtu.be/YE7VzlLtp-4", 1),
    ],
)
async def test_additional_sites_in_inline_queries_are_private_only(
    chat_type: enums.ChatType, url: str, expected_results: int,
) -> None:
    class Store:
        async def chat(self, chat_id: int, kind: str) -> ChatSettings:
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Query:
        def __init__(self) -> None:
            self.query = url
            self.chat_type = chat_type
            self.from_user = SimpleNamespace(id=123)
            self.results = None

        async def answer(self, results: list, **kwargs: object) -> None:
            self.results = results

    settings = SimpleNamespace(
        whitelist=frozenset(),
        site=lambda _: SimpleNamespace(disabled=False),
    )
    query = Query()
    inline = Inline(SimpleNamespace(), settings, Store())
    await inline.query(SimpleNamespace(), query)
    assert query.results is not None
    assert len(query.results) == expected_results
    if expected_results:
        task_id, pending = next(iter(inline.pending.items()))
        assert pending.public_group == (
            chat_type == enums.ChatType.SUPERGROUP
        )
        button = query.results[0].reply_markup.inline_keyboard[0][0]
        assert button.text == "Download"
        assert button.callback_data == f"inline:download:{task_id}"


@pytest.mark.asyncio
async def test_inline_download_button_starts_without_chosen_feedback() -> None:
    events = []
    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), SimpleNamespace())
    task_id = bot.inline.add(123, Request("youtube", "id", "https://youtu.be/id"))

    async def deliver(pending, user_id, inline_message_id):
        events.append((pending.request.content_id, user_id, inline_message_id))

    bot.inline._deliver = deliver

    class Callback:
        data = f"inline:download:{task_id}"
        inline_message_id = "inline-message"
        message = None
        from_user = SimpleNamespace(id=123)

        async def answer(self, text=None, **kwargs):
            events.append(("answer", text))

    await bot.on_callback(None, Callback())
    assert events == [
        ("answer", "Downloading media…"),
        ("id", 123, "inline-message"),
    ]
    assert task_id not in bot.inline.pending


@pytest.mark.asyncio
async def test_inline_download_button_rejects_other_user() -> None:
    replies = []
    inline = Inline(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    task_id = inline.add(123, Request("youtube", "id", "https://youtu.be/id"))

    class Callback:
        data = f"inline:download:{task_id}"
        inline_message_id = "inline-message"
        from_user = SimpleNamespace(id=999)

        async def answer(self, text=None, **kwargs):
            replies.append(text)

    assert await inline.callback(Callback())
    assert task_id in inline.pending
    assert replies == ["This download has started or expired. Send the query again if needed."]


@pytest.mark.asyncio
async def test_group_inline_cannot_reuse_cached_marked_media() -> None:
    edits = []

    class Client:
        async def edit_inline_media(self, *args, **kwargs):
            raise AssertionError("marked media must not be sent")

        async def edit_inline_text(self, message_id, text, **kwargs):
            edits.append(text)

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, True, 10, False)

        async def cached_media(self, extractor_id, content_id):
            return Media(
                "youtube", "id", "https://youtu.be/YE7VzlLtp-4", nsfw=True,
                items=[MediaItem(kind="video", file_id="cached", video_codec="avc")],
            )

    client = Client()
    inline = Inline(client, SimpleNamespace(caching=True), Store())
    inline.runner = SimpleNamespace()
    task_id = inline.add(
        123, Request("youtube", "id", "https://youtu.be/YE7VzlLtp-4"), public_group=True,
    )
    chosen = SimpleNamespace(
        result_id=task_id, from_user=SimpleNamespace(id=123), inline_message_id="message",
    )
    await inline.chosen(client, chosen)
    assert edits == [
        "⚠️ This link is unavailable here. Send it to PyVD in a DM or private group."
    ]
