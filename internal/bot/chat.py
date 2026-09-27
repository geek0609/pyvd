"""Chat visibility used by group delivery rules."""

from hydrogram import enums, types


def is_public_group(chat: types.Chat) -> bool:
    return chat.type == enums.ChatType.SUPERGROUP and bool(chat.username)
