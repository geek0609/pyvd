import time
from types import SimpleNamespace

import pytest
from hydrogram import enums

from internal.bot.inline import Inline
from internal.extractors.sites import Request
from internal.models.media import ChatSettings, Media, MediaItem


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
        assert next(iter(inline.pending.values())).public_group == (
            chat_type == enums.ChatType.SUPERGROUP
        )


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
