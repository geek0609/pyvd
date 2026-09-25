class MediaError(Exception):
    """A download or upload failure that can be shown to a user."""


class FileTooLarge(MediaError):
    pass


class DurationTooLong(MediaError):
    pass


class NoMedia(MediaError):
    pass
