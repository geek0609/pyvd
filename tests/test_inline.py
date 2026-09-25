import time
from types import SimpleNamespace

from internal.bot.inline import Inline
from internal.extractors.sites import Request


def test_inline_tasks_expire_and_belong_to_user() -> None:
    inline = Inline(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    request = Request("youtube", "id", "https://youtu.be/id")
    task_id = inline.add(123, request)
    assert inline.pop(task_id, 999) is None
    task_id = inline.add(123, request)
    assert inline.pop(task_id, 123) == request
    task_id = inline.add(123, request)
    inline.pending[task_id].expires = time.monotonic() - 1
    assert inline.pop(task_id, 123) is None
